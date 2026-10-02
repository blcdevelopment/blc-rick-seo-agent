"""Fetch an audit's pages through Firecrawl when the audited site's security blocks our browser.

Fallback only. crawl_site comes here when, and only when, the HOMEPAGE was kept from our own
browser by the audited site's security (``crawler.site_security_block``: its bot check never let
the browser through, only a person could clear it, or it refused the browser with HTTP 401/403),
a Firecrawl key is set and the allowances are on. Every other site, and every other failure
(404/410/429/5xx, a timeout, DNS, a blocked internal page on a reachable site), keeps the browser
crawl exactly as before.

Firecrawl's cloud browser fetches each page from its own addresses, in standard mode only: the
default proxy, no stealth mode, no CAPTCHA solving, one attempt per page, one page at a time. Its
answers become the same CrawledPage / CrawlResult the browser crawl builds, so everything
downstream (SEO, UX/UI and lead-gen facts, social discovery, the site-health link inventory,
PageSpeed, the report) runs unchanged on the real pages:

- ``html``: Firecrawl's ``rawHtml``, the page's full HTML, <head> included (Firecrawl's ``html``
  format drops the head, so it is not used). For a page Firecrawl's browser rendered it is the
  document after the page's scripts ran, like the browser's ``page.content()``; when Firecrawl
  answers from a plain fetch or its cache instead, it is the HTML the server sent. Every analyser
  reads only this HTML, so they all run on the real markup unchanged.
- ``final_url`` and ``status_code``: Firecrawl's metadata (``url`` after redirects, ``statusCode``).
- ``title``: the document's <title>, as the browser's ``document.title`` reads it.
- ``text``: the body's text from the HTML, without scripts, styles, noscript, templates and SVG.
  It only feeds ``text_length`` in the crawl JSON; the browser's ``inner_text`` would also leave
  out what CSS hides, which the HTML alone cannot tell.
- the screenshot: Firecrawl's full-page screenshot (the browser path takes a full-page one too),
  downloaded at once because Firecrawl's links expire, into the same file the browser path writes,
  size-capped and checked to be a PNG or JPEG. Firecrawl's desktop window may be wider than our
  1280 px. Screenshots are kept for operators; no report shows them.
- which pages: the browser path's own discovery and ranking (``discover_internal_links`` on the
  homepage HTML, then ``select_child_pages`` with robots.txt), so the same pages are chosen.
  Firecrawl's ``links`` format is not requested: a flat list of addresses has none of the
  nav/header/footer context the ranking scores.
- ``frame_form_count`` / ``frame_form_field_count`` = 0 and ``axe_results`` = None. Both need a
  live page (forms inside iframes; the optional axe pass, off in this deployment), and 0 / None
  are what the analysers already treat as "not measured": the UX extractor still credits a form
  embed from its provider signature in the HTML, and the accessibility advisory has no result for
  these pages.
- ``passed_interstitial`` = None (our browser passed no check). Every page and the crawl carry
  ``fetched_via = "firecrawl"``, and the crawl names what blocked the browser.

Not routed through Firecrawl (cost): robots.txt still comes from this server ("unavailable" when
the check answers it, which applies no rules). The site-health sweep sends nothing from this
server for such a crawl (site_health reads ``fetched_via``): every request would meet the same
wall, and a plain 403 would read as broken pages. It keeps its on-page checks over the fetched
pages and reports ``partial: bot_blocked``. PageSpeed is fetched by Google from the pages' real
addresses, unchanged.

Caps, all counted atomically in Redis before anything is sent (one credit per page, its
screenshot included): at most ``crawler_firecrawl_max_pages`` pages per audit, at most
``crawler_firecrawl_daily_limit`` fallback audits per UTC day, and never more than
``crawler_firecrawl_monthly_page_limit`` pages per UTC month (an audit reserves its full page cap
up front and gives back what it did not send).

The audit fails with the browser's original plain message when Firecrawl is shown the site's
check too (the crawler's own markers on its final address, status or title, a meta refresh into a
check's address, a check's own markup, or a 202) or answers anything but 200 for the homepage,
when Firecrawl itself fails or answers more than the size caps, when an allowance is used up or
cannot be counted, and when the address is not a public one. The key is sent only to Firecrawl,
over https, in a header, and never logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import redis
from bs4 import BeautifulSoup
from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings
from apps.worker.stages.crawler import (
    BOT_CHECK_PAGE_REASON,
    OTHER_SITE_BOT_CHECK_PAGE_REASON,
    CrawledPage,
    CrawlerError,
    CrawlResult,
    RobotsPolicy,
    SiteBlockedError,
    _screenshot_path,
    _utc_now,
    assert_crawlable_url,
    discover_internal_links,
    http_error_page_reason,
    interstitial_reason,
    is_failed_http_status,
    is_same_site,
    select_child_pages,
    site_security_block,
)

logger = logging.getLogger(__name__)

FETCHED_VIA = "firecrawl"
# The reasons the report's "Failed internal pages" table prints for a page Firecrawl could not
# fetch, or that led to another website: plain, and about that one page.
PAGE_FETCH_FAILED_REASON = "Could not load this page"
OFF_SITE_PAGE_REASON = "Leads to another website"

_SCRAPE_PATH = "/v1/scrape"
# The request waits this much longer than Firecrawl's own timeout, so Firecrawl answers first.
_REQUEST_SLACK_SECONDS = 15
# No new page is started after this long, so a slow service can never push the audit toward the
# Celery soft limit (each page is bounded by its own timeout as well).
_TOTAL_BUDGET_SECONDS = 300
# Firecrawl answers after which every further page would fail the same way (a bad key, no
# credits, a refused site, its rate limit): the remaining pages are skipped, not sent.
_STOP_STATUSES = frozenset({401, 402, 403, 429})
# Firecrawl answers an operator must see (a bad key, the credits used up): logged at WARNING.
_WARN_STATUSES = frozenset({401, 402})
# Outcomes that mean a cap or the counter stopped the fallback: logged at WARNING too.
_WARN_OUTCOMES = frozenset({"daily_limit_reached", "monthly_limit_reached", "counter_unavailable"})

# Size caps. A real page's rawHtml is about 1 MB (tchomesmn.com: 0.93 MB); parsing 5 MB already
# takes the analysers close to a minute, so a bigger answer is refused, not read.
_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_RAW_HTML_MAX_CHARS = 5_000_000

_SCREENSHOT_MAX_BYTES = 20 * 1024 * 1024
# The whole screenshot download, not just one read (httpx's timeouts are per operation).
_SCREENSHOT_TIMEOUT_SECONDS = 30
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)

# Redis counters: fallback audits per UTC day, pages per UTC month. Each expires on its own.
_KEY_PREFIX = "firecrawl_fallback"
_DAY_KEY_TTL_SECONDS = 2 * 24 * 60 * 60
_MONTH_KEY_TTL_SECONDS = 40 * 24 * 60 * 60

# Bot checks Firecrawl can only recognise by their markup (it reports no response headers):
# strings that only the checks' own pages carry, all of a row required. Deliberately not the bare
# /cdn-cgi/challenge-platform/ prefix (Cloudflare also loads .../scripts/jsd/main.js from it on
# ordinary pages) nor a bare awswaf.com (pages that use AWS WAF's JavaScript integration load its
# script too): only the challenge pages' own options objects and endpoints.
_MARKUP_CHECKS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "SiteGround anti-bot check",
        (re.compile(r"""sgsubmit_url\s*=\s*["']/\.well-known/sgcaptcha/"""),),
    ),
    # Cloudflare's challenge page sets _cf_chl_opt with a challenge type and loads its
    # orchestrate/chl_page script. A Turnstile widget on an ordinary page (a contact form's spam
    # check) sets _cf_chl_opt too, but with neither of those: found on tchomesmn.com/contact-us,
    # 2 October 2026, which stopped a real audit after its second page.
    (
        "Cloudflare challenge",
        (
            re.compile(r"\b_cf_chl_opt\b"),
            re.compile(
                r"""cType\s*:\s*["'](?:managed|non-interactive|interactive)["']"""
                r"""|/cdn-cgi/challenge-platform/[^"'\s]*orchestrate/chl_page/"""
            ),
        ),
    ),
    ("AWS WAF challenge", (re.compile(r"\bgokuProps\b"), re.compile(r"\.awswaf\.com/"))),
    ("Vercel challenge", (re.compile(r"""["']/\.well-known/vercel/security/"""),)),
)
# SiteGround's and AWS WAF's checks answer 202; a real page answers 200.
_CHALLENGE_STATUS = 202


class FirecrawlError(Exception):
    """Firecrawl did not return a page. The message names only a status or the kind of failure:
    never the key, a header, or Firecrawl's own error text."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ScreenshotError(Exception):
    """A screenshot that could not be stored; the message is recorded as screenshot_error."""


@dataclass(frozen=True)
class _Fetched:
    """One page as Firecrawl returned it, with what the crawl needs read off its HTML."""

    final_url: str
    status_code: int | None
    title: str | None
    html: str
    text: str
    screenshot: str | None
    # The bot check a meta refresh in the page leads into (SiteGround's 202 answer is nothing
    # but a refresh to /.well-known/sgcaptcha/), or None.
    refresh_check: str | None


@dataclass
class _Stats:
    """What the one-line ``firecrawl_fallback`` log event reports."""

    blocked_by: str
    outcome: str = "error"
    requests: int = 0
    pages: int = 0
    failed: int = 0
    skipped: int = 0
    detail: str = ""
    # Set by a Firecrawl answer an operator must see (_WARN_STATUSES).
    warn: bool = False


def _api_key(settings: Settings) -> str:
    key = settings.firecrawl_api_key
    return key.get_secret_value().strip() if key is not None else ""


def is_enabled(settings: Settings) -> bool:
    """True when the fallback may run: a Firecrawl key is set and neither limit is 0."""
    return (
        bool(_api_key(settings))
        and settings.crawler_firecrawl_daily_limit > 0
        and settings.crawler_firecrawl_monthly_page_limit > 0
    )


def max_pages_per_audit(settings: Settings) -> int:
    """Pages one fallback audit may fetch: never more than a browser crawl would open."""
    return min(settings.crawler_max_pages, settings.crawler_firecrawl_max_pages)


def scrape_request_body(url: str, settings: Settings) -> dict[str, Any]:
    """The /v1/scrape request for one page. Standard mode only: the basic proxy, Firecrawl's own
    browser, no actions. Ads and cookie banners are left in place and TLS errors ignored, as in
    our browser (``ignore_https_errors``), so the page is the one a visitor's browser shows."""
    formats = ["rawHtml"]
    if settings.crawler_screenshots_enabled:
        formats.append("screenshot@fullPage")
    return {
        "url": url,
        "formats": formats,
        "proxy": "basic",
        "blockAds": False,
        "skipTlsVerification": True,
        "timeout": settings.crawler_firecrawl_timeout_seconds * 1000,
    }


def _request_timeout_seconds(settings: Settings) -> float:
    """How long one /v1/scrape exchange may take in all: Firecrawl's own timeout plus slack."""
    return float(settings.crawler_firecrawl_timeout_seconds + _REQUEST_SLACK_SECONDS)


def _redis_client(settings: Settings) -> Any:
    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5)


@dataclass(frozen=True)
class Allowance:
    """A reservation against the caps: the month's page key (to give unused pages back), or the
    cap that refused it (``refused``), in which case nothing stayed taken."""

    month_key: str
    pages: int
    refused: str | None = None


def reserve_allowance(settings: Settings, pages: int, now: datetime | None = None) -> Allowance:
    """Take one fallback audit from today's allowance and ``pages`` pages from this month's, in
    one MULTI/EXEC (INCR / INCRBY are atomic, so concurrent audits never share the last slot).
    Over either cap, both are given back at once and the cap is named: a reservation is granted
    only when the counts it leaves are within the caps, so the month's total never goes past
    ``crawler_firecrawl_monthly_page_limit``."""
    now = now or datetime.now(UTC)
    day_key = f"{_KEY_PREFIX}:audits:{now:%Y-%m-%d}"
    month_key = f"{_KEY_PREFIX}:pages:{now:%Y-%m}"
    client = _redis_client(settings)
    try:
        with client.pipeline(transaction=True) as pipe:
            pipe.incr(day_key)
            pipe.expire(day_key, _DAY_KEY_TTL_SECONDS)
            pipe.incrby(month_key, pages)
            pipe.expire(month_key, _MONTH_KEY_TTL_SECONDS)
            audits, _day_ttl_set, used, _month_ttl_set = pipe.execute()
        refused = None
        if int(audits) > settings.crawler_firecrawl_daily_limit:
            refused = "daily_limit_reached"
        elif int(used) > settings.crawler_firecrawl_monthly_page_limit:
            refused = "monthly_limit_reached"
        if refused is not None:
            with client.pipeline(transaction=True) as pipe:
                pipe.decr(day_key)
                pipe.decrby(month_key, pages)
                pipe.execute()
    finally:
        client.close()
    return Allowance(month_key=month_key, pages=pages, refused=refused)


def give_back_pages(settings: Settings, allowance: Allowance, pages: int) -> None:
    """Return reserved pages that were never sent to Firecrawl to the month's allowance."""
    if pages <= 0:
        return
    client = _redis_client(settings)
    try:
        client.decrby(allowance.month_key, pages)
    finally:
        client.close()


def _is_public(url: str, cache: dict[str, bool]) -> bool:
    """Firecrawl is only ever sent public http(s) addresses, whatever crawler_allow_private_hosts
    says (that switch is for local crawls of fixtures, which Firecrawl could not reach anyway)."""
    host = (urlparse(url).hostname or "").lower()
    if host not in cache:
        try:
            assert_crawlable_url(url, allow_private_hosts=False)
        except CrawlerError:
            cache[host] = False
        else:
            cache[host] = True
    return cache[host]


def _document_title(soup: BeautifulSoup) -> str | None:
    """The document's title as ``document.title`` reads it: the first <title> that is not an
    SVG image's own, whitespace collapsed."""
    for tag in soup.find_all("title"):
        if tag.find_parent("svg") is None:
            return " ".join(tag.get_text().split()) or None
    return None


def _refresh_check(soup: BeautifulSoup, base_url: str) -> str | None:
    """The bot check a meta refresh leads into: its target is a check's own address."""
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).strip().lower() != "refresh":
            continue
        _delay, _sep, target = str(meta.get("content", "")).partition(";")
        target = target.strip()
        if target[:4].lower() == "url=":
            target = target[4:].strip()
        target = target.strip("'\"")
        if not target:
            continue
        found = interstitial_reason(urljoin(base_url, target), None, None, None)
        if found is not None:
            return found[0]
    return None


def _body_text(soup: BeautifulSoup) -> str:
    """The page's text, like the browser's body inner_text minus what only CSS hides. Runs last
    on ``soup``: it removes elements from it."""
    root = soup.body or soup
    for tag in root(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return " ".join(root.get_text(" ", strip=True).split())


def _read_page(url: str, payload: Any) -> _Fetched:
    """Turn Firecrawl's answer into a _Fetched, or raise FirecrawlError for an unusable one."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise FirecrawlError("answer was not a success")
    if not isinstance(data, dict):
        raise FirecrawlError("answer carried no page")
    html = data.get("rawHtml")
    if not isinstance(html, str) or not html.strip():
        raise FirecrawlError("answer carried no HTML")
    if len(html) > _RAW_HTML_MAX_CHARS:
        raise FirecrawlError("page HTML too large")
    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    status = metadata.get("statusCode")
    status_code = status if isinstance(status, int) and not isinstance(status, bool) else None
    final_url = next(
        (
            value.strip()
            for value in (metadata.get("url"), metadata.get("sourceURL"))
            if isinstance(value, str) and value.strip()
        ),
        url,
    )
    screenshot = data.get("screenshot")
    soup = BeautifulSoup(html, "html.parser")
    refresh_check = _refresh_check(soup, final_url)
    return _Fetched(
        final_url=final_url,
        status_code=status_code,
        # Not metadata's title, which may come from og:title: the browser reads only <title>.
        title=_document_title(soup),
        html=html,
        text=_body_text(soup),
        screenshot=screenshot if isinstance(screenshot, str) and screenshot else None,
        refresh_check=refresh_check,
    )


async def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    """The response body, or None once it is (declared or streamed) past ``limit`` bytes."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _scrape(client: httpx.AsyncClient, url: str, settings: Settings) -> _Fetched:
    body = scrape_request_body(url, settings)
    try:
        # The whole exchange, not just one read: httpx's timeouts are per operation.
        async with (
            asyncio.timeout(_request_timeout_seconds(settings)),
            client.stream("POST", _SCRAPE_PATH, json=body) as response,
        ):
            if response.status_code != 200:
                raise FirecrawlError(
                    f"HTTP {response.status_code}", status_code=response.status_code
                )
            content = await _read_capped(response, _RESPONSE_MAX_BYTES)
    except (TimeoutError, httpx.TimeoutException):
        raise FirecrawlError("timed out") from None
    except httpx.HTTPError as exc:
        raise FirecrawlError(f"request failed ({type(exc).__name__})") from None
    if content is None:
        raise FirecrawlError("answer too large")
    try:
        payload = json.loads(content)
    except ValueError:
        raise FirecrawlError("answer was not JSON") from None
    return _read_page(url, payload)


async def _try_scrape(
    client: httpx.AsyncClient, url: str, settings: Settings, stats: _Stats
) -> _Fetched | FirecrawlError:
    stats.requests += 1
    try:
        return await _scrape(client, url, settings)
    except SoftTimeLimitExceeded:
        raise
    except FirecrawlError as exc:
        if exc.status_code in _WARN_STATUSES:
            stats.warn = True
        return exc
    except Exception as exc:
        return FirecrawlError(f"unexpected error ({type(exc).__name__})")


def _bot_check(fetched: _Fetched) -> str | None:
    """The bot check Firecrawl was shown instead of the page. Firecrawl reports no response
    headers, so the crawler's own markers decide from the address and title, then a meta refresh
    into a check's address, a check's own markup (_MARKUP_CHECKS), and a 202."""
    found = interstitial_reason(fetched.final_url, fetched.status_code, None, fetched.title)
    if found is not None:
        return found[0]
    if fetched.refresh_check is not None:
        return fetched.refresh_check
    for label, patterns in _MARKUP_CHECKS:
        if all(pattern.search(fetched.html) for pattern in patterns):
            return label
    if fetched.status_code == _CHALLENGE_STATUS:
        return f"unnamed check (HTTP {_CHALLENGE_STATUS})"
    return None


def _image_suffix(data: bytes) -> str | None:
    for signature, suffix in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return suffix
    return None


def _decode_data_uri(uri: str) -> bytes:
    header, sep, encoded = uri.partition(",")
    header = header.lower()
    if not sep or not header.startswith("data:image/") or ";base64" not in header:
        raise _ScreenshotError("screenshot data is not a base64 image")
    if len(encoded) > _SCREENSHOT_MAX_BYTES * 4 // 3 + 4:
        raise _ScreenshotError("screenshot is too large")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _ScreenshotError("screenshot data is not valid base64") from None


async def _download_image(url: str, settings: Settings) -> bytes:
    """Download Firecrawl's screenshot: https only, a public host only, no redirects, at most
    _SCREENSHOT_MAX_BYTES. No Firecrawl key is sent with it."""
    if urlparse(url).scheme != "https":
        raise _ScreenshotError("screenshot address is not https")
    try:
        assert_crawlable_url(url, allow_private_hosts=False)
    except CrawlerError:
        raise _ScreenshotError("screenshot address is not a public host") from None
    try:
        async with (
            asyncio.timeout(_SCREENSHOT_TIMEOUT_SECONDS),
            httpx.AsyncClient(
                headers={"User-Agent": settings.crawler_user_agent},
                timeout=_SCREENSHOT_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise _ScreenshotError(f"screenshot download answered HTTP {response.status_code}")
            data = await _read_capped(response, _SCREENSHOT_MAX_BYTES)
    except (TimeoutError, httpx.TimeoutException):
        raise _ScreenshotError("screenshot download timed out") from None
    if data is None:
        raise _ScreenshotError("screenshot is too large")
    return data


async def _store_screenshot(
    screenshot: str | None, settings: Settings, audit_id: str | None, page_url: str
) -> tuple[str | None, str | None]:
    """Save Firecrawl's screenshot where the browser path saves its own. Returns
    ``(screenshot_path, screenshot_error)`` like the browser path; never raises (bar the soft
    time limit)."""
    if not settings.crawler_screenshots_enabled:
        return None, None
    if not screenshot:
        return None, "no screenshot was returned"
    try:
        if screenshot.startswith("data:"):
            data = _decode_data_uri(screenshot)
        else:
            data = await _download_image(screenshot, settings)
        suffix = _image_suffix(data)
        if suffix is None:
            raise _ScreenshotError("screenshot is not a PNG or JPEG image")
        path = _screenshot_path(settings, audit_id, page_url).with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except SoftTimeLimitExceeded:
        raise
    except _ScreenshotError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"screenshot could not be saved ({type(exc).__name__})"
    return str(path), None


async def _page(
    fetched: _Fetched,
    *,
    url: str,
    settings: Settings,
    audit_id: str | None,
    source_url: str | None = None,
    link_score: float | None = None,
) -> CrawledPage:
    screenshot_path, screenshot_error = await _store_screenshot(
        fetched.screenshot, settings, audit_id, fetched.final_url
    )
    return CrawledPage(
        url=url,
        final_url=fetched.final_url,
        status_code=fetched.status_code,
        title=fetched.title,
        html=fetched.html,
        text=fetched.text,
        fetched_at=_utc_now(),
        source_url=source_url,
        link_score=link_score,
        screenshot_path=screenshot_path,
        screenshot_error=screenshot_error,
        fetched_via=FETCHED_VIA,
    )


def _api_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.firecrawl_api_url,
        headers={"Authorization": f"Bearer {_api_key(settings)}"},
        timeout=httpx.Timeout(_request_timeout_seconds(settings), connect=10.0),
        follow_redirects=False,
    )


def _skipped(url: str, reason: str, source_url: str) -> dict[str, Any]:
    return {"url": url, "status": "skipped", "reason": reason, "source_url": source_url}


def _failed(url: str, reason: str, source_url: str) -> dict[str, Any]:
    return {"url": url, "status": "failed", "reason": reason, "source_url": source_url}


async def crawl_site_via_firecrawl(
    url: str,
    *,
    start_url: str,
    robots: RobotsPolicy,
    settings: Settings,
    audit_id: str | None,
    started_at: str,
    blocked: SiteBlockedError,
) -> CrawlResult:
    """Crawl the site through Firecrawl after its security blocked our browser on the homepage
    (``blocked``). Returns the CrawlResult the browser crawl would have, or raises ``blocked``
    itself (the visitor's original plain message) when the fallback cannot help. Logs one
    ``firecrawl_fallback`` line per call: WARNING when a cap stopped it or Firecrawl refused the
    key or ran out of credits, INFO otherwise."""
    stats = _Stats(blocked_by=site_security_block(blocked) or "unknown")
    started = time.monotonic()
    try:
        result = await _crawl(
            url,
            start_url=start_url,
            robots=robots,
            settings=settings,
            audit_id=audit_id,
            started_at=started_at,
            blocked=blocked,
            stats=stats,
        )
        stats.outcome = result.status
        return result
    except SoftTimeLimitExceeded:
        stats.outcome = "timed_out"
        raise
    except CrawlerError:
        # ``blocked`` itself, or the redirect checks: final, plain text already.
        raise
    except Exception as exc:
        # Anything unexpected must not reach the visitor as raw text: the original message.
        stats.outcome, stats.detail = "error", type(exc).__name__
        raise blocked from None
    finally:
        warn = stats.warn or stats.outcome in _WARN_OUTCOMES
        logger.log(
            logging.WARNING if warn else logging.INFO,
            "firecrawl_fallback audit_id=%s host=%s blocked_by=%r outcome=%s%s requests=%d "
            "pages=%d failed=%d skipped=%d seconds=%.1f",
            audit_id or "-",
            urlparse(start_url).hostname or "-",
            stats.blocked_by,
            stats.outcome,
            f" ({stats.detail})" if stats.detail else "",
            stats.requests,
            stats.pages,
            stats.failed,
            stats.skipped,
            time.monotonic() - started,
        )


async def _crawl(
    url: str,
    *,
    start_url: str,
    robots: RobotsPolicy,
    settings: Settings,
    audit_id: str | None,
    started_at: str,
    blocked: SiteBlockedError,
    stats: _Stats,
) -> CrawlResult:
    public_hosts: dict[str, bool] = {}
    if not _is_public(start_url, public_hosts):
        stats.outcome = "not_public"
        raise blocked

    max_pages = max_pages_per_audit(settings)
    # Reserved before the first request: one audit from today's allowance and the full page cap
    # from this month's. What is not sent is given back at the end.
    try:
        allowance: Allowance | None = reserve_allowance(settings, max_pages)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        allowance = None
    if allowance is None:
        # Without the counters the caps cannot be kept, so there is no fallback.
        stats.outcome = "counter_unavailable"
        raise blocked
    if allowance.refused is not None:
        stats.outcome = allowance.refused
        raise blocked

    try:
        return await _fetch_pages(
            url,
            start_url=start_url,
            robots=robots,
            settings=settings,
            audit_id=audit_id,
            started_at=started_at,
            blocked=blocked,
            stats=stats,
            max_pages=max_pages,
            public_hosts=public_hosts,
        )
    finally:
        try:
            give_back_pages(settings, allowance, max_pages - stats.requests)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            # The unused pages stay counted: the month's cap errs on the safe side.
            logger.warning("firecrawl_fallback could not give back unused pages")


async def _fetch_pages(
    url: str,
    *,
    start_url: str,
    robots: RobotsPolicy,
    settings: Settings,
    audit_id: str | None,
    started_at: str,
    blocked: SiteBlockedError,
    stats: _Stats,
    max_pages: int,
    public_hosts: dict[str, bool],
) -> CrawlResult:
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
    failed_pages: list[dict[str, Any]] = []
    skipped_pages: list[dict[str, Any]] = []

    async with _api_client(settings) as client:
        home = await _try_scrape(client, start_url, settings, stats)
        if isinstance(home, FirecrawlError):
            stats.outcome, stats.detail = "service_error", str(home)
            raise blocked
        if _bot_check(home) is not None or home.status_code != 200:
            # Firecrawl met the same wall, or the homepage answered it anything but a page
            # (Firecrawl reports no headers, so a header-only check shows only as its status).
            # One attempt only: no stealth mode, no retry.
            stats.outcome = "still_blocked"
            stats.detail = f"HTTP {home.status_code}" if home.status_code != 200 else ""
            raise blocked
        if not is_same_site(start_url, home.final_url):
            stats.outcome = "left_site"
            raise CrawlerError("Homepage redirected outside the starting site.")
        # The same re-check of the post-redirect address as the browser crawl.
        assert_crawlable_url(home.final_url, settings.crawler_allow_private_hosts)

        homepage = await _page(home, url=start_url, settings=settings, audit_id=audit_id)
        pages = [homepage]
        discovered_links = discover_internal_links(homepage.html, homepage.final_url)
        targets = select_child_pages(
            discovered_links, robots, settings, max_pages, homepage.final_url, skipped_pages
        )

        # Why the pages not opened yet are skipped: the site's own check walled a page (each
        # further page would meet it), Firecrawl refused service, or the time budget ran out.
        stop_reason: str | None = None
        for candidate in targets:
            if stop_reason is None and time.monotonic() >= deadline:
                stop_reason = "time_budget_reached"
            if stop_reason is not None:
                skipped_pages.append(_skipped(candidate.url, stop_reason, homepage.final_url))
                continue
            if not _is_public(candidate.url, public_hosts):
                failed_pages.append(
                    _failed(candidate.url, PAGE_FETCH_FAILED_REASON, homepage.final_url)
                )
                continue
            fetched = await _try_scrape(client, candidate.url, settings, stats)
            if isinstance(fetched, FirecrawlError):
                failed_pages.append(
                    _failed(candidate.url, PAGE_FETCH_FAILED_REASON, homepage.final_url)
                )
                stats.detail = str(fetched)
                if fetched.status_code in _STOP_STATUSES:
                    stop_reason = "stopped_after_service_error"
                continue
            reason, walled = _child_page_problem(candidate.url, fetched, public_hosts)
            if reason is not None:
                failed_pages.append(_failed(candidate.url, reason, homepage.final_url))
                if walled:
                    stop_reason = "stopped_after_bot_check"
                continue
            pages.append(
                await _page(
                    fetched,
                    url=candidate.url,
                    settings=settings,
                    audit_id=audit_id,
                    source_url=homepage.final_url,
                    link_score=candidate.score,
                )
            )

    stats.pages, stats.failed, stats.skipped = len(pages), len(failed_pages), len(skipped_pages)
    return CrawlResult(
        requested_url=url,
        start_url=start_url,
        final_url=homepage.final_url,
        status="partial" if failed_pages or skipped_pages else "complete",
        pages=pages,
        discovered_links=discovered_links,
        skipped_pages=skipped_pages,
        failed_pages=failed_pages,
        robots=robots,
        started_at=started_at,
        completed_at=_utc_now(),
        max_pages=max_pages,
        user_agent=settings.crawler_user_agent,
        fetched_via=FETCHED_VIA,
        browser_blocked_by=stats.blocked_by,
    )


def _child_page_problem(
    url: str, fetched: _Fetched, public_hosts: dict[str, bool]
) -> tuple[str | None, bool]:
    """Why an internal page Firecrawl fetched is not scored (None when it is), and whether it
    walls the rest (the site's own check)."""
    if _bot_check(fetched) is not None:
        if is_same_site(url, fetched.final_url):
            return BOT_CHECK_PAGE_REASON, True
        return OTHER_SITE_BOT_CHECK_PAGE_REASON, False
    if is_failed_http_status(fetched.status_code):
        return http_error_page_reason(fetched.status_code), False
    if fetched.status_code != 200:
        return PAGE_FETCH_FAILED_REASON, False
    # Another website's page (a redirect away) is never scored as the audited site's.
    if not is_same_site(url, fetched.final_url):
        return OFF_SITE_PAGE_REASON, False
    if not _is_public(fetched.final_url, public_hosts):
        return PAGE_FETCH_FAILED_REASON, False
    return None, False
