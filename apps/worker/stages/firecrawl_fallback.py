"""Fetch an audit's pages through Firecrawl when the audited site's security blocks our browser.

Fallback only. crawl_site comes here when, and only when, the HOMEPAGE was kept from our own
browser by the audited site's security (``crawler.site_security_block``: its bot check never let
the browser through, only a person could clear it, or it refused the browser with HTTP 401/403),
a Firecrawl key is set and the daily allowance is on. Every other site, and every other failure
(404/410/429/5xx, a timeout, DNS, a blocked internal page on a reachable site), keeps the browser
crawl exactly as before.

Firecrawl's cloud browser fetches each page from its own addresses, in standard mode only: the
default proxy, no stealth mode, no CAPTCHA solving, one attempt per page, one page at a time. Its
answers become the same CrawledPage / CrawlResult the browser crawl builds, so everything
downstream (SEO, UX/UI and lead-gen facts, social discovery, the site-health link inventory,
PageSpeed, the report) runs unchanged on the real pages:

- ``html``: Firecrawl's ``rawHtml``, the rendered document as the browser's ``page.content()``
  gives it, <head> included (Firecrawl's ``html`` format drops the head, so it is not used). Every
  analyser reads only this HTML, so they all run on the real markup unchanged.
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

Not routed through Firecrawl (cost): robots.txt and the site-health sweep still come from this
server and degrade as before (robots "unavailable", the sweep ``partial: bot_blocked``). PageSpeed
is fetched by Google from the pages' real addresses, unchanged.

The audit fails with the browser's original plain message when Firecrawl is shown the site's
check too (the crawler's own markers on its final address, status or title, or a meta refresh into
a check's address) or a 401/403, when Firecrawl itself fails, when the day's allowance is used up
or cannot be counted, and when the address is not a public one. The key is sent only to Firecrawl,
in a header, and never logged.
"""

from __future__ import annotations

import base64
import binascii
import logging
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
    _http_error,
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
# The reason the report's "Failed internal pages" table prints for a page Firecrawl could not
# fetch: plain, and about that one page.
PAGE_FETCH_FAILED_REASON = "Could not load this page"

_SCRAPE_PATH = "/v1/scrape"
# The request waits this much longer than Firecrawl's own timeout, so Firecrawl answers first.
_REQUEST_SLACK_SECONDS = 15
# No new page is started after this long, so a slow service can never push the audit toward the
# Celery soft limit (each page is bounded by its own timeout as well).
_TOTAL_BUDGET_SECONDS = 300
# Firecrawl answers after which every further page would fail the same way (a bad key, no
# credits, a refused site, its rate limit): the remaining pages are skipped, not sent.
_STOP_STATUSES = frozenset({401, 402, 403, 429})

_SCREENSHOT_MAX_BYTES = 20 * 1024 * 1024
_SCREENSHOT_TIMEOUT_SECONDS = 30
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)

# One Redis counter per UTC day: INCR is atomic, so two audits never share the last slot.
_DAILY_KEY_PREFIX = "firecrawl_fallback"
_DAILY_KEY_TTL_SECONDS = 2 * 24 * 60 * 60


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


def _api_key(settings: Settings) -> str:
    key = settings.firecrawl_api_key
    return key.get_secret_value().strip() if key is not None else ""


def is_enabled(settings: Settings) -> bool:
    """True when the fallback may run: a Firecrawl key is set and the daily limit is not 0."""
    return bool(_api_key(settings)) and settings.crawler_firecrawl_daily_limit > 0


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


def _redis_client(settings: Settings) -> Any:
    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5)


def count_fallback_audit(settings: Settings, now: datetime | None = None) -> int:
    """Count one more fallback audit for today (UTC) and return today's total. The dated key
    expires after about two days, so no clean-up is needed."""
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    key = f"{_DAILY_KEY_PREFIX}:{day}"
    client = _redis_client(settings)
    try:
        with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, _DAILY_KEY_TTL_SECONDS)
            count, _expiry_set = pipe.execute()
    finally:
        client.close()
    return int(count)


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


async def _scrape(client: httpx.AsyncClient, url: str, settings: Settings) -> _Fetched:
    try:
        response = await client.post(_SCRAPE_PATH, json=scrape_request_body(url, settings))
    except httpx.TimeoutException:
        raise FirecrawlError("timed out") from None
    except httpx.HTTPError as exc:
        raise FirecrawlError(f"request failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise FirecrawlError(f"HTTP {response.status_code}", status_code=response.status_code)
    try:
        payload = response.json()
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
        return exc
    except Exception as exc:
        return FirecrawlError(f"unexpected error ({type(exc).__name__})")


def _bot_check(fetched: _Fetched) -> str | None:
    """The bot check Firecrawl was shown instead of the page, by the crawler's own markers.
    Firecrawl reports no response headers, so the address, status and title decide, plus a meta
    refresh into a check's address."""
    found = interstitial_reason(fetched.final_url, fetched.status_code, None, fetched.title)
    return found[0] if found is not None else fetched.refresh_check


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
    chunks: list[bytes] = []
    total = 0
    async with (
        httpx.AsyncClient(
            headers={"User-Agent": settings.crawler_user_agent},
            timeout=_SCREENSHOT_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != 200:
            raise _ScreenshotError(f"screenshot download answered HTTP {response.status_code}")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _SCREENSHOT_MAX_BYTES:
            raise _ScreenshotError("screenshot is too large")
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _SCREENSHOT_MAX_BYTES:
                raise _ScreenshotError("screenshot is too large")
            chunks.append(chunk)
    return b"".join(chunks)


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
        timeout=httpx.Timeout(
            settings.crawler_firecrawl_timeout_seconds + _REQUEST_SLACK_SECONDS, connect=10.0
        ),
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
    ``firecrawl_fallback`` line per call."""
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
        # ``blocked`` itself, an HTTP error or the redirect check: final, plain text already.
        raise
    except Exception as exc:
        # Anything unexpected must not reach the visitor as raw text: the original message.
        stats.outcome, stats.detail = "error", type(exc).__name__
        raise blocked from None
    finally:
        logger.info(
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

    # Counted before the first request, so an audit that spends credits always counts.
    try:
        today: int | None = count_fallback_audit(settings)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        today = None
    if today is None:
        # Without the count the daily allowance cannot be kept, so there is no fallback.
        stats.outcome = "counter_unavailable"
        raise blocked
    if today > settings.crawler_firecrawl_daily_limit:
        stats.outcome = "daily_limit_reached"
        raise blocked

    max_pages = min(settings.crawler_max_pages, settings.crawler_firecrawl_max_pages)
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
    failed_pages: list[dict[str, Any]] = []
    skipped_pages: list[dict[str, Any]] = []

    async with _api_client(settings) as client:
        home = await _try_scrape(client, start_url, settings, stats)
        if isinstance(home, FirecrawlError):
            stats.outcome, stats.detail = "service_error", str(home)
            raise blocked
        if _bot_check(home) is not None or home.status_code in {401, 403}:
            # Firecrawl met the same wall. One attempt only: no stealth mode, no retry.
            stats.outcome = "still_blocked"
            raise blocked
        if is_failed_http_status(home.status_code):
            stats.outcome = "http_error"
            raise _http_error(home.status_code)
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
            if _bot_check(fetched) is not None:
                own_site = is_same_site(candidate.url, fetched.final_url)
                reason = BOT_CHECK_PAGE_REASON if own_site else OTHER_SITE_BOT_CHECK_PAGE_REASON
                failed_pages.append(_failed(candidate.url, reason, homepage.final_url))
                if own_site:
                    stop_reason = "stopped_after_bot_check"
                continue
            if is_failed_http_status(fetched.status_code):
                failed_pages.append(
                    _failed(
                        candidate.url,
                        http_error_page_reason(fetched.status_code),
                        homepage.final_url,
                    )
                )
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
