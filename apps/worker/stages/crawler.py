from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag
from celery.exceptions import SoftTimeLimitExceeded
from playwright import async_api as playwright_api

from apps.shared.config import Settings
from apps.worker.stages.extractor_uxui import _EMBED_PROVIDER_SIGNATURES


class CrawlerError(RuntimeError):
    """Raised when the audit cannot collect the homepage."""


class SiteBlockedError(CrawlerError):
    """The site's bot check, its firewall or an HTTP error kept the audit browser off the page.

    ``str(exc)`` is the whole-audit text: when the homepage is blocked, the visitor who ran the
    audit reads it word for word (tasks.py stores it as the job's ``error_message``), so it stays
    short, plain and non-technical. ``page_reason`` describes one page only: crawl_site records it
    for an internal page that failed inside an audit that went on, and the report prints it in its
    "Failed internal pages" table. ``bot_check`` names the audited site's own bot check when that
    is what blocked the page (None for an HTTP error, or for another website's check).
    ``status_code`` is the HTTP error status the audited site itself answered (None for a bot
    check, and for an error answered by another website a redirect led to)."""

    def __init__(
        self,
        message: str,
        *,
        page_reason: str | None = None,
        bot_check: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.page_reason = page_reason or message
        self.bot_check = bot_check
        self.status_code = status_code


# Shown when a bot check did not let the browser through (it never cleared, or only a person
# could clear it). The report is never built from the check's own page.
BOT_CHECK_BLOCKED_MESSAGE = (
    "We couldn't audit this website. Its security check blocked our scanner, so we stopped "
    "instead of scoring the security screen. If this is your site, ask your web host to allow "
    "our scanner, then try again."
)
# The same two cases for ONE internal page of an audit that went on: the report prints these
# next to the page's address, so they never speak of the whole audit.
BOT_CHECK_PAGE_REASON = "Blocked by the site's security check"
OTHER_SITE_BOT_CHECK_PAGE_REASON = "Leads to another website's security check"


def http_error_message(status_code: int) -> str:
    """Plain visitor text for a page that answered with an HTTP error status."""
    if status_code in {401, 403}:
        return (
            f"We couldn't audit this website. It refused our scanner (HTTP {status_code}), "
            "usually a firewall or security setting. If this is your site, ask your web host to "
            "allow our scanner, then try again."
        )
    if status_code == 429:
        return "The website asked us to slow down (HTTP 429). Please try again in a few minutes."
    if status_code in {404, 410}:
        return (
            f"We couldn't find that page (HTTP {status_code}). Please check the address and try "
            "again."
        )
    return f"The website returned an error (HTTP {status_code}). Please try again later."


def http_error_page_reason(status_code: int) -> str:
    """Short reason for one internal page that answered with an HTTP error status."""
    if status_code in {401, 403}:
        return f"Refused by the site (HTTP {status_code})"
    if status_code == 429:
        return "The site asked us to slow down (HTTP 429)"
    if status_code in {404, 410}:
        return f"Page not found (HTTP {status_code})"
    return f"The site returned an error (HTTP {status_code})"


def _http_error(status_code: int, *, own_site: bool = True) -> SiteBlockedError:
    """A page that answered with an HTTP error. ``own_site`` is False when a redirect led to
    another website that answered it: the visitor's text is the same, but no ``status_code`` is
    recorded, so another website's 401/403 never reads as the audited site's refusal."""
    return SiteBlockedError(
        http_error_message(status_code),
        page_reason=http_error_page_reason(status_code),
        status_code=status_code if own_site else None,
    )


def site_security_block(exc: SiteBlockedError) -> str | None:
    """What kept the browser off a page when it was the audited site's own security: the label
    of its bot check (one the browser could not wait out, or one only a person can clear), or
    "HTTP 401" / "HTTP 403" for a refusal. None for anything else (another website's check,
    404/410/429/5xx), which the Firecrawl fallback never covers."""
    if exc.bot_check:
        return exc.bot_check
    if exc.status_code in {401, 403}:
        return f"HTTP {exc.status_code}"
    return None


def _bot_check_error(label: str, *, own_site: bool) -> SiteBlockedError:
    """A page blocked by a bot check: the audited site's own (``bot_check`` names it, and
    crawl_site stops opening pages) or another website's that a link led to (no wall)."""
    if own_site:
        return SiteBlockedError(
            BOT_CHECK_BLOCKED_MESSAGE, page_reason=BOT_CHECK_PAGE_REASON, bot_check=label
        )
    return SiteBlockedError(BOT_CHECK_BLOCKED_MESSAGE, page_reason=OTHER_SITE_BOT_CHECK_PAGE_REASON)


# Bot checks ("interstitials") that some hosts answer with instead of the page. Only exact,
# vendor-documented markers count, so a normal page can never match: cf-ray and
# "server: cloudflare" are on every proxied response and are deliberately NOT markers.
INTERSTITIAL_WAITABLE = "waitable"  # the site's own script can clear it and load the page
INTERSTITIAL_TERMINAL = "terminal"  # only a person can clear it (a CAPTCHA or a block page)

_SITEGROUND_CHECK = "SiteGround anti-bot check"
_CLOUDFLARE_CHECK = "Cloudflare challenge"
# (path prefix, vendor label, kind). SiteGround's check falls back to its human CAPTCHA page.
_INTERSTITIAL_PATHS: tuple[tuple[str, str, str], ...] = (
    ("/.well-known/sgcaptcha/", _SITEGROUND_CHECK, INTERSTITIAL_WAITABLE),
    ("/.well-known/captcha/", "SiteGround CAPTCHA", INTERSTITIAL_TERMINAL),
    ("/cdn-cgi/challenge-platform/", _CLOUDFLARE_CHECK, INTERSTITIAL_WAITABLE),
)
# (header name, exact lower-cased value, vendor label, kind). SiteGround's `sg-captcha` header
# is matched on "contains challenge" in interstitial_reason itself.
_INTERSTITIAL_HEADERS: tuple[tuple[str, str, str, str], ...] = (
    ("cf-mitigated", "challenge", _CLOUDFLARE_CHECK, INTERSTITIAL_WAITABLE),
    ("x-amzn-waf-action", "challenge", "AWS WAF challenge", INTERSTITIAL_WAITABLE),
    ("x-amzn-waf-action", "captcha", "AWS WAF CAPTCHA", INTERSTITIAL_TERMINAL),
    ("x-vercel-mitigated", "challenge", "Vercel challenge", INTERSTITIAL_WAITABLE),
)
# Exact, lower-cased document titles.
_INTERSTITIAL_TITLES: dict[str, tuple[str, str]] = {
    "robot challenge screen": (_SITEGROUND_CHECK, INTERSTITIAL_WAITABLE),
    "just a moment...": (_CLOUDFLARE_CHECK, INTERSTITIAL_WAITABLE),
    "attention required! | cloudflare": ("Cloudflare block page", INTERSTITIAL_TERMINAL),
}


def interstitial_reason(
    url: str | None,
    status: int | None,
    headers: Mapping[str, str] | None,
    title: str | None,
) -> tuple[str, str] | None:
    """Name the bot check a response shows in place of the page, or None for a real page.

    Returns ``(vendor label, kind)``, kind being INTERSTITIAL_WAITABLE (the site's own script
    may still let the browser through) or INTERSTITIAL_TERMINAL (only a person can). Terminal
    markers win, because SiteGround's human-CAPTCHA page still carries its challenge header and
    title. ``status`` never decides on its own: a marker-less 403 is left to the HTTP-status
    path, and a 200 or 202 that carries a marker is still a check."""
    lowered = {
        str(name).lower(): str(value).strip().lower() for name, value in (headers or {}).items()
    }
    path = urlparse(url or "").path
    found: list[tuple[str, str]] = [
        (label, kind) for prefix, label, kind in _INTERSTITIAL_PATHS if path.startswith(prefix)
    ]
    if "challenge" in lowered.get("sg-captcha", ""):
        found.append((_SITEGROUND_CHECK, INTERSTITIAL_WAITABLE))
    found.extend(
        (label, kind)
        for name, value, label, kind in _INTERSTITIAL_HEADERS
        if lowered.get(name) == value
    )
    by_title = _INTERSTITIAL_TITLES.get((title or "").strip().lower())
    if by_title is not None:
        found.append(by_title)
    for reason in found:
        if reason[1] == INTERSTITIAL_TERMINAL:
            return reason
    return found[0] if found else None


def is_interstitial_url(url: str | None) -> bool:
    """True for a bot check's own address (SiteGround's /.well-known/sgcaptcha/ and
    /.well-known/captcha/, Cloudflare's /cdn-cgi/challenge-platform/): never a page to score."""
    path = urlparse(url or "").path
    return any(path.startswith(prefix) for prefix, _label, _kind in _INTERSTITIAL_PATHS)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_url(url: str, base_url: str | None = None) -> str | None:
    joined = urljoin(base_url, url) if base_url else url
    joined = urldefrag(joined).url.strip()
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None

    try:
        parsed_port = parsed.port
    except ValueError:
        return None

    port = ""
    if parsed_port and not (
        (parsed.scheme == "http" and parsed_port == 80)
        or (parsed.scheme == "https" and parsed_port == 443)
    ):
        port = f":{parsed_port}"

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((parsed.scheme.lower(), f"{host}{port}", path, "", parsed.query, ""))


def _site_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_same_site(start_url: str, candidate_url: str) -> bool:
    return _site_host(start_url) == _site_host(candidate_url)


IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip_is_blocked(ip_address: IpAddress) -> bool:
    return any(
        (
            ip_address.is_private,
            ip_address.is_loopback,
            ip_address.is_link_local,
            ip_address.is_multicast,
            ip_address.is_reserved,
            ip_address.is_unspecified,
        )
    )


def _hostname_is_private(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered in {"localhost"} or lowered.endswith(".localhost") or lowered.endswith(".local"):
        return True

    try:
        ip_address = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        return False

    return _ip_is_blocked(ip_address)


def _resolve_host_ips(hostname: str) -> list[IpAddress]:
    resolved: list[IpAddress] = []
    for info in socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP):
        raw_address = str(info[4][0]).split("%")[0]
        try:
            resolved.append(ipaddress.ip_address(raw_address))
        except ValueError:
            continue
    return resolved


def assert_crawlable_url(url: str, allow_private_hosts: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise CrawlerError("Only HTTP and HTTPS URLs can be crawled.")
    if parsed.hostname is None:
        raise CrawlerError("URL must include a hostname.")
    if allow_private_hosts:
        return
    if _hostname_is_private(parsed.hostname):
        raise CrawlerError("Private, local, and reserved hosts are not crawlable by default.")

    # Resolve DNS so a public hostname that points at a private or cloud-metadata IP
    # (e.g. 127.0.0.1 or 169.254.169.254) is rejected before any navigation happens.
    try:
        resolved = _resolve_host_ips(parsed.hostname)
    except OSError as exc:
        raise CrawlerError(f"Could not resolve host '{parsed.hostname}': {exc}") from exc
    if not resolved:
        raise CrawlerError(f"Host '{parsed.hostname}' did not resolve to any IP address.")
    if any(_ip_is_blocked(ip_address) for ip_address in resolved):
        raise CrawlerError(
            "Host resolves to a private, loopback, link-local, or reserved IP address "
            "and is not crawlable by default."
        )


async def _host_blocked_for_subrequest(
    host: str | None,
    settings: Settings,
    resolve_cache: dict[str, bool],
) -> bool:
    """Decide whether a sub-resource/redirect request to ``host`` should be aborted by
    the request-level SSRF guard. This mirrors ``assert_crawlable_url`` but runs per
    request during rendering, closing the gap where the page-level check covers only the
    navigation target. Returns False (allow) when interception is off or the crawler is
    explicitly permitted to reach private hosts (local dev / QA crawls)."""
    if settings.crawler_allow_private_hosts or not settings.crawler_intercept_requests:
        return False
    if not host:
        return False
    host = host.lower().rstrip(".")
    # Cheap literal check first (no DNS): IP literals, localhost, *.local.
    if _hostname_is_private(host):
        return True
    if host in resolve_cache:
        return resolve_cache[host]
    # DNS resolution is blocking; run it off the event loop and memoize per crawl so a
    # page with many sub-resources doesn't re-resolve the same host each time.
    loop = asyncio.get_running_loop()
    try:
        resolved = await loop.run_in_executor(None, _resolve_host_ips, host)
    except OSError:
        # An unresolvable sub-resource will fail at the network layer anyway; don't let
        # the guard itself block it (the navigation target was already validated).
        resolve_cache[host] = False
        return False
    blocked = bool(resolved) and any(_ip_is_blocked(ip_address) for ip_address in resolved)
    resolve_cache[host] = blocked
    return blocked


def _make_ssrf_route_guard(settings: Settings, resolve_cache: dict[str, bool]):
    """Build a Playwright route handler that aborts requests to blocked hosts and lets
    everything else through. Every intercepted request MUST be resolved (continue/abort)
    exactly once or the page hangs, so the handler is written to never raise."""

    async def _guard(route: playwright_api.Route) -> None:
        block = False
        try:
            host = urlparse(route.request.url).hostname
            block = await _host_blocked_for_subrequest(host, settings, resolve_cache)
        except Exception:
            block = False
        with suppress(Exception):
            if block:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

    return _guard


async def _new_crawl_context(
    browser: playwright_api.Browser,
    settings: Settings,
    resolve_cache: dict[str, bool],
    storage_state: Any = None,
) -> playwright_api.BrowserContext:
    """Create a browser context with the standard crawl options and, unless private
    hosts are allowed, the request-level SSRF guard attached.

    ``storage_state`` carries the cookies a site's bot check set on the homepage (crawl_site
    passes it only after the browser passed such a check), so child pages are not challenged
    again. It is handed to Playwright only when given: every other site gets exactly the
    options below."""
    options: dict[str, Any] = {
        "ignore_https_errors": True,
        "service_workers": "block",
        "user_agent": settings.crawler_user_agent,
        "viewport": {"width": 1280, "height": 720},
    }
    if storage_state is not None:
        options["storage_state"] = storage_state
    context = await browser.new_context(**options)
    context.set_default_timeout(settings.crawler_page_timeout_seconds * 1000)
    context.set_default_navigation_timeout(settings.crawler_page_timeout_seconds * 1000)
    if settings.crawler_intercept_requests and not settings.crawler_allow_private_hosts:
        await context.route("**/*", _make_ssrf_route_guard(settings, resolve_cache))
    return context


def is_failed_http_status(status_code: int | None) -> bool:
    return status_code is not None and status_code >= 400


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    text: str
    score: float
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "text": self.text,
            "score": round(self.score, 3),
            "sources": self.sources,
        }


@dataclass(frozen=True)
class CrawledPage:
    url: str
    final_url: str
    status_code: int | None
    title: str | None
    html: str
    text: str
    fetched_at: str
    source_url: str | None = None
    link_score: float | None = None
    screenshot_path: str | None = None
    screenshot_error: str | None = None
    # Raw axe-core result for the optional advisory accessibility pass (P2-15b). In-memory
    # ONLY (deliberately NOT serialized in to_public_dict): it is consumed by the advisory
    # normalizer in tasks.py right after the crawl, and never reaches the scoring path.
    axe_results: dict[str, Any] | None = None
    # Forms found inside child iframes by the post-capture frame pass (popup/lazy-embed
    # forms are invisible in the page HTML). In-memory only, consumed by the UX extractor;
    # counted AFTER page.content() so the captured HTML/screenshot stay unchanged.
    frame_form_count: int = 0
    frame_form_field_count: int = 0
    # The bot check (e.g. "SiteGround anti-bot check") the browser waited out before this page
    # loaded, or None. Operator traceability only: stored with the crawl JSON, never shown in a
    # report or to visitors.
    passed_interstitial: str | None = None
    # "firecrawl" when the page was fetched through Firecrawl because the site's security blocked
    # our browser (firecrawl_fallback.py); None for the browser crawl. Stored with the crawl JSON
    # (only when set, so a browser crawl's JSON is unchanged).
    fetched_via: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        data = {
            "url": self.url,
            "final_url": self.final_url,
            "status": "success",
            "status_code": self.status_code,
            "title": self.title,
            "html_length": len(self.html),
            "text_length": len(self.text),
            "fetched_at": self.fetched_at,
            "source_url": self.source_url,
            "link_score": self.link_score,
            "screenshot_path": self.screenshot_path,
            "screenshot_error": self.screenshot_error,
            "passed_interstitial": self.passed_interstitial,
        }
        if self.fetched_via is not None:
            data["fetched_via"] = self.fetched_via
        return data


@dataclass(frozen=True)
class RobotsPolicy:
    status: str
    robots_url: str | None
    error: str | None = None
    parser: RobotFileParser | None = None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "robots_url": self.robots_url,
            "error": self.error,
        }


@dataclass
class CrawlResult:
    requested_url: str
    start_url: str
    final_url: str
    status: str
    pages: list[CrawledPage]
    discovered_links: list[LinkCandidate]
    skipped_pages: list[dict[str, Any]]
    failed_pages: list[dict[str, Any]]
    robots: RobotsPolicy
    started_at: str
    completed_at: str
    max_pages: int
    user_agent: str
    # Set only when the site's security blocked our browser on the homepage and the pages were
    # fetched through Firecrawl instead: "firecrawl", and what blocked the browser (a bot check's
    # label or "HTTP 403"). Operator traceability; a browser crawl's JSON carries neither key.
    fetched_via: str | None = None
    browser_blocked_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "requested_url": self.requested_url,
            "start_url": self.start_url,
            "final_url": self.final_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "max_pages": self.max_pages,
            "user_agent": self.user_agent,
            "robots": self.robots.to_dict(),
            "summary": {
                "successful_pages": len(self.pages),
                "failed_pages": len(self.failed_pages),
                "skipped_pages": len(self.skipped_pages),
                "discovered_internal_links": len(self.discovered_links),
            },
            "pages": [page.to_public_dict() for page in self.pages],
            "failed_pages": self.failed_pages,
            "skipped_pages": self.skipped_pages,
            "discovered_links": [link.to_dict() for link in self.discovered_links],
        }
        if self.fetched_via is not None:
            data["fetched_via"] = self.fetched_via
            data["browser_blocked_by"] = self.browser_blocked_by
        return data


def _tag_has_ancestor(tag: Tag, names: set[str], tokens: set[str] | None = None) -> bool:
    tokens = tokens or set()
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        parent_name = (parent.name or "").lower()
        if parent_name in names:
            return True
        values = " ".join(
            str(value)
            for attr in ("id", "class", "role", "aria-label")
            for value in (
                parent.get(attr, [])
                if isinstance(parent.get(attr), list)
                else [parent.get(attr, "")]
            )
        ).lower()
        if any(token in values for token in tokens):
            return True
    return False


def discover_internal_links(homepage_html: str, base_url: str) -> list[LinkCandidate]:
    soup = BeautifulSoup(homepage_html, "html.parser")
    candidates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"score": 0.0, "text": "", "sources": set()}
    )

    for order, anchor in enumerate(soup.find_all("a", href=True)):
        href = str(anchor.get("href", "")).strip()
        normalized = normalize_url(href, base_url)
        if normalized is None or not is_same_site(base_url, normalized):
            continue
        if normalized == normalize_url(base_url):
            continue

        text = " ".join(anchor.get_text(" ", strip=True).split())
        sources: set[str] = candidates[normalized]["sources"]
        score = 1.0

        if _tag_has_ancestor(anchor, {"nav"}, {"nav", "menu"}):
            score += 3.0
            sources.add("nav")
        if _tag_has_ancestor(anchor, {"header"}, {"header"}):
            score += 1.5
            sources.add("header")
        if _tag_has_ancestor(anchor, {"footer"}, {"footer"}):
            score += 1.0
            sources.add("footer")
        if _tag_has_ancestor(anchor, {"main", "section"}, {"hero", "primary"}):
            score += 1.0
            sources.add("body")
        if text:
            score += 0.5

        depth = len([part for part in urlparse(normalized).path.split("/") if part])
        score -= min(depth, 6) * 0.2
        score += max(0.0, 1.0 - (order / 1000.0))

        candidates[normalized]["score"] += score
        if text and not candidates[normalized]["text"]:
            candidates[normalized]["text"] = text[:120]

    return sorted(
        (
            LinkCandidate(
                url=url,
                text=str(data["text"]),
                score=float(data["score"]),
                sources=sorted(data["sources"]),
            )
            for url, data in candidates.items()
        ),
        key=lambda candidate: (-candidate.score, candidate.url),
    )


async def load_robots_policy(start_url: str, settings: Settings) -> RobotsPolicy:
    if not settings.crawler_respect_robots_txt:
        return RobotsPolicy(status="disabled", robots_url=None)

    parsed = urlparse(start_url)
    robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    timeout = min(settings.crawler_page_timeout_seconds, 10)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
            timeout=timeout,
        ) as client:
            response = await client.get(robots_url)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return RobotsPolicy(status="unavailable", robots_url=robots_url, error=str(exc))

    bot_check = interstitial_reason(str(response.url), response.status_code, response.headers, None)
    if bot_check is not None:
        # A bot check answered instead of robots.txt. Its HTML parses to zero rules, which must
        # not be recorded as a loaded robots.txt that allows everything.
        return RobotsPolicy(
            status="unavailable",
            robots_url=robots_url,
            error=f"robots.txt answered with a bot check ({bot_check[0]})",
        )
    if response.status_code == 404:
        return RobotsPolicy(status="missing", robots_url=robots_url)
    if response.status_code >= 400:
        return RobotsPolicy(
            status="unavailable",
            robots_url=robots_url,
            error=f"robots.txt returned HTTP {response.status_code}",
        )

    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines())
    return RobotsPolicy(status="loaded", robots_url=robots_url, parser=parser)


def _screenshot_path(settings: Settings, audit_id: str | None, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    folder = settings.local_screenshot_storage_dir / (audit_id or "manual")
    return folder / f"{digest}.png"


def _browser_cache_roots() -> list[Path]:
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        return [Path(configured)]
    return [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "AppData" / "Local" / "ms-playwright",
    ]


def _find_installed_chromium_executable() -> Path | None:
    patterns = (
        # Full Chromium (most reliable to launch with an explicit executable_path).
        # Modern Playwright uses chrome-linux64; chrome-linux is kept for older layouts.
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-win/chrome.exe",
        # Headless shell — what chromium.launch(headless=True) uses by default on
        # Playwright >= 1.49, so it must be discoverable by the fallback too.
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
    )
    for root in _browser_cache_roots():
        if not root.exists():
            continue
        for pattern in patterns:
            matches = sorted(root.glob(pattern), reverse=True)
            for candidate in matches:
                if candidate.exists():
                    return candidate
    return None


async def _launch_chromium(playwright: Any, settings: Settings) -> Any:
    executable_path = settings.crawler_chromium_executable_path
    if executable_path is not None:
        if not executable_path.exists():
            raise CrawlerError(f"Configured Chromium executable does not exist: {executable_path}")
        return await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable_path),
        )

    try:
        return await playwright.chromium.launch(headless=True)
    except playwright_api.Error as exc:
        fallback = _find_installed_chromium_executable()
        if fallback is None:
            raise CrawlerError(f"Could not launch Chromium: {exc}") from exc
        return await playwright.chromium.launch(headless=True, executable_path=str(fallback))


async def _capture_screenshot(
    page: playwright_api.Page,
    settings: Settings,
    audit_id: str | None,
    url: str,
) -> tuple[str | None, str | None]:
    if not settings.crawler_screenshots_enabled:
        return None, None

    path = _screenshot_path(settings, audit_id, url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return None, str(exc)
    return str(path), None


# How often the page is looked at while a site's own bot check runs, and how long the real page
# may take to settle once the check lets the browser through.
_INTERSTITIAL_POLL_SECONDS = 0.25
_SETTLE_TIMEOUT_MS = 5000
# What _interstitial_now reports while the check's own navigation replaces the document.
_STILL_NAVIGATING = ("", "navigating")


class _DocumentWatch:
    """The main frame's document responses during one page load.

    goto() resolves with the FIRST document, which on a bot-checked site is the check's own
    page (SiteGround answers 202 with a meta refresh), and a fast CPU can pass the check before
    the first look. So this keeps the latest non-redirect document of the main frame, plus the
    label of the first bot check seen on any document of the load."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self.latest: Any = None
        self.seen: str | None = None
        self.crashed = False

    def on_response(self, response: Any) -> None:
        # An event callback must never raise into Playwright's event dispatch.
        with suppress(Exception):
            if not response.request.is_navigation_request():
                return
            if response.frame != self._page.main_frame or 300 <= response.status < 400:
                return
            self.latest = response
            if self.seen is None:
                found = interstitial_reason(response.url, response.status, response.headers, None)
                if found is not None:
                    self.seen = found[0]

    def on_crash(self, _page: Any) -> None:
        self.crashed = True


async def _interstitial_now(
    page: Any, documents: _DocumentWatch, response: Any
) -> tuple[str, str] | None:
    """The bot check the page shows right now, _STILL_NAVIGATING while it is mid-navigation,
    or None for a real page."""
    try:
        title = await page.title()
    except playwright_api.Error:
        if documents.crashed or page.is_closed():
            # A crashed or closed page is not navigating: fail now, not after the whole budget.
            raise
        # title() fails while a navigation (the check's own redirect) replaces the document.
        # If the latest document is itself a check (its address or headers say so), the check
        # is still in the way; otherwise the page is just navigating.
        current = documents.latest or response
        if current is not None:
            found = interstitial_reason(current.url, current.status, current.headers, None)
            if found is not None:
                return found
        return _STILL_NAVIGATING
    # Read the latest document only now: it may have changed while title() was awaited.
    current = documents.latest or response
    status = current.status if current is not None else None
    headers = current.headers if current is not None else None
    return interstitial_reason(page.url, status, headers, title)


async def _settled_look(
    page: Any, documents: _DocumentWatch, response: Any
) -> tuple[str, str] | None:
    """Let the page that replaced a bot check finish loading, then look at it again."""
    with suppress(playwright_api.Error):
        await page.wait_for_load_state("domcontentloaded", timeout=_SETTLE_TIMEOUT_MS)
    with suppress(playwright_api.Error):
        await page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT_MS)
    return await _interstitial_now(page, documents, response)


def _on_another_site(url: str, page: Any, current: Any) -> bool:
    """True when the page asked for has ended up on another website (an internal link to a
    client portal on a third party's host, say). The page's address and its latest document
    must both be elsewhere, so a navigation caught half-way never counts."""
    addresses = [page.url] + ([current.url] if current is not None else [])
    return not any(is_same_site(url, address) for address in addresses)


async def _wait_out_interstitial(
    page: Any,
    url: str,
    response: Any,
    documents: _DocumentWatch,
    settings: Settings,
) -> tuple[Any, str | None]:
    """Give a site's own bot check up to ``crawler_challenge_wait_seconds`` to let the browser
    through, the way a visitor's browser simply waits.

    Returns the final document's response and the label of the check that was passed (None
    when there was none). Raises SiteBlockedError at once for a check only a person can clear
    (a CAPTCHA or block page) and for another website's check (``url`` led there; that site is
    not being audited), and when the site's own check is still showing after the budget.
    Nothing is disguised or solved, and a blocked page is never retried: each attempt is one
    more unsolved check on this server's record with the host."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, settings.crawler_challenge_wait_seconds)
    current = await _interstitial_now(page, documents, response)
    if current is None and documents.seen is not None:
        # The check cleared before the first look (a fast CPU passes it inside the networkidle
        # wait, which may have ended mid-load): settle like any other passed check.
        current = await _settled_look(page, documents, response)
    if current is None:
        return documents.latest or response, documents.seen
    label = documents.seen
    # Whether the check is, as far as we know, still in the way. A page caught mid-navigation
    # counts as checking only when a check showed up during this load.
    checking = label is not None
    while True:
        elsewhere = _on_another_site(url, page, documents.latest or response)
        if current is not _STILL_NAVIGATING:
            if elsewhere:
                raise _bot_check_error(current[0], own_site=False)
            label = label or current[0]
            checking = True
            if current[1] == INTERSTITIAL_TERMINAL:
                raise _bot_check_error(current[0], own_site=True)
        if loop.time() >= deadline:
            if checking:
                raise _bot_check_error(label or current[0], own_site=not elsewhere)
            # No check in the way, just a page still navigating: capture it as before.
            return documents.latest or response, label
        await asyncio.sleep(_INTERSTITIAL_POLL_SECONDS)
        current = await _interstitial_now(page, documents, response)
        if current is None:
            # The check let the browser through: let the real page settle, then look again.
            checking = False
            current = await _settled_look(page, documents, response)
            if current is None:
                return documents.latest or response, label


async def _render_page(
    context: playwright_api.BrowserContext,
    url: str,
    settings: Settings,
    audit_id: str | None,
    source_url: str | None = None,
    link_score: float | None = None,
) -> CrawledPage:
    page = await context.new_page()
    try:
        documents = _DocumentWatch(page)
        # Registered before goto, so even the first document (a bot check's own page) is seen.
        page.on("response", documents.on_response)
        page.on("crash", documents.on_crash)
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=settings.crawler_page_timeout_seconds * 1000,
        )
        with suppress(playwright_api.TimeoutError):
            await page.wait_for_load_state("networkidle", timeout=5000)

        # A site's bot check (SiteGround, Cloudflare, ...) may answer first. Wait for the site's
        # own check BEFORE the status check, so a page reached after it records its own status
        # (200), not the check's (202), and the check's page is never captured or scored.
        response, passed_interstitial = await _wait_out_interstitial(
            page, url, response, documents, settings
        )

        status_code = response.status if response else None
        if is_failed_http_status(status_code):
            raise _http_error(status_code, own_site=not _on_another_site(url, page, response))

        html = await page.content()
        title = await page.title()
        # One last look at what was captured: a check that only its title gives away (the page
        # was still navigating when the wait ended) must not be scored either.
        captured = interstitial_reason(
            page.url, status_code, response.headers if response else None, title
        )
        if captured is not None:
            raise _bot_check_error(captured[0], own_site=not _on_another_site(url, page, response))
        text = " ".join((await page.locator("body").inner_text(timeout=3000)).split())
        screenshot_path, screenshot_error = await _capture_screenshot(
            page,
            settings,
            audit_id,
            page.url,
        )

        # Optional advisory accessibility pass (P2-15b). Runs only when enabled; the helper
        # never raises (graceful per-page skip on missing asset / error / timeout), and the
        # raw result is carried in-memory only and never reaches scoring.
        axe_results = None
        if settings.accessibility_advisory_enabled:
            from apps.worker.stages.accessibility import run_axe_on_page

            axe_results = await run_axe_on_page(page, settings)

        # Capture the URL before the frame pass: its scrolling can fire scrollspy
        # history/hash updates, and final_url must describe the same state as the
        # HTML/text/screenshot captured above.
        final_url = page.url
        # Popup/lazy-iframe lead forms never appear in page.content(); a bounded frame
        # pass counts them so the UX extractor can credit "form present" honestly.
        frame_form_count, frame_form_field_count = await _scan_frames_for_forms(page, html)

        return CrawledPage(
            url=url,
            final_url=final_url,
            status_code=status_code,
            title=title.strip() or None,
            html=html,
            text=text,
            fetched_at=_utc_now(),
            source_url=source_url,
            link_score=link_score,
            screenshot_path=screenshot_path,
            screenshot_error=screenshot_error,
            axe_results=axe_results,
            frame_form_count=frame_form_count,
            frame_form_field_count=frame_form_field_count,
            passed_interstitial=passed_interstitial,
        )
    except playwright_api.TimeoutError as exc:
        raise CrawlerError(f"Timed out rendering {url}") from exc
    except SoftTimeLimitExceeded:
        # Celery's soft limit subclasses Exception: without this re-raise the broad handler
        # below would wrap it into CrawlerError, the honest timed-out failure path in tasks.py
        # would never run, and the worker would run on to the hard-limit SIGKILL.
        raise
    except CrawlerError:
        # Already a final, plain message (a blocked site, an HTTP error). Wrapping it again as
        # "Could not render <url>: ..." is what used to put raw text in front of visitors.
        raise
    except Exception as exc:
        raise CrawlerError(f"Could not render {url}: {exc}") from exc
    finally:
        await page.close()


# Flattened provider tokens for the frame-pass precheck — the popup/lazy embeds the scan
# exists to count always leave either an <iframe> tag or their provider's loader script in
# the static HTML (signature list owned by extractor_uxui; one object, so a new provider
# signature extends detection and the precheck together).
_EMBED_SIGNATURE_TOKENS: tuple[str, ...] = tuple(
    token for _, tokens in _EMBED_PROVIDER_SIGNATURES for token in tokens
)


def _may_have_lazy_embed(html: str) -> bool:
    """Static-HTML precheck for the frame pass: a lazy iframe carries an ``iframe`` token and
    a popup/JS-mounted embed carries its provider's loader script — no signal at all means
    there is nothing for the scroll nudge to wake up."""
    lowered = (html or "").lower()
    return "iframe" in lowered or any(token in lowered for token in _EMBED_SIGNATURE_TOKENS)


async def _scan_frames_for_forms(
    page: Any, html: str, timeout_seconds: float = 3.0
) -> tuple[int, int]:
    """Count <form> elements (and their fields) inside child frames, nudging lazy iframes
    into loading with a quick scroll first. Runs AFTER the page HTML/screenshot are
    captured, so it cannot change any other extracted fact. Skipped outright when the page
    has no child frame and no embed signal in its static HTML — the scroll nudge costs
    ~1-3s of browser time per page, which an iframe-less site should not pay. Best-effort
    with a hard time cap: any failure returns (0, 0) and the audit continues (graceful,
    like PSI/axe); only the worker's soft time limit propagates."""
    if len(page.frames) <= 1 and not _may_have_lazy_embed(html):
        return 0, 0
    try:
        async with asyncio.timeout(timeout_seconds):
            with suppress(playwright_api.Error, asyncio.TimeoutError):
                await page.evaluate(
                    "async () => {"
                    " const h = document.body ? document.body.scrollHeight : 0;"
                    " for (const y of [0.25, 0.5, 0.75, 1]) {"
                    "   window.scrollTo(0, h * y);"
                    "   await new Promise(r => setTimeout(r, 200));"
                    " }"
                    " window.scrollTo(0, 0);"
                    "}"
                )
            with suppress(playwright_api.TimeoutError):
                await page.wait_for_load_state("networkidle", timeout=1000)
            forms = 0
            fields = 0
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    counts = await frame.evaluate(
                        "() => {"
                        " const forms = document.querySelectorAll('form');"
                        " let inputs = 0;"
                        " forms.forEach((el) => {"
                        "   inputs += el.querySelectorAll('input,textarea,select').length;"
                        " });"
                        " return [forms.length, inputs];"
                        "}"
                    )
                    forms += int(counts[0] or 0)
                    fields += int(counts[1] or 0)
                except SoftTimeLimitExceeded:
                    raise
                except Exception:
                    # Cross-origin/permission edge cases: skip the frame, keep the rest.
                    continue
            return forms, fields
    except SoftTimeLimitExceeded:
        # The worker is out of time: propagate so the task can mark the job failed
        # honestly instead of the hard limit killing it mid-pipeline (site_health and
        # google_search_console follow the same convention).
        raise
    except Exception:
        return 0, 0


def select_child_pages(
    discovered_links: list[LinkCandidate],
    robots: RobotsPolicy,
    settings: Settings,
    max_pages: int,
    source_url: str,
    skipped_pages: list[dict[str, Any]],
) -> list[LinkCandidate]:
    """The internal pages to open after the homepage: the best-ranked links, up to
    ``max_pages - 1``, minus those robots.txt disallows (recorded in ``skipped_pages``)."""
    targets: list[LinkCandidate] = []
    for candidate in discovered_links:
        if len(targets) >= max(max_pages - 1, 0):
            break
        if not robots.can_fetch(settings.crawler_user_agent, candidate.url):
            skipped_pages.append(
                {
                    "url": candidate.url,
                    "status": "skipped",
                    "reason": "disallowed_by_robots_txt",
                    "source_url": source_url,
                }
            )
            continue
        targets.append(candidate)
    return targets


async def crawl_site(url: str, settings: Settings, audit_id: str | None = None) -> CrawlResult:
    started_at = _utc_now()
    start_url = normalize_url(url)
    if start_url is None:
        raise CrawlerError("Audit URL is not a crawlable HTTP/HTTPS URL.")
    assert_crawlable_url(start_url, settings.crawler_allow_private_hosts)

    robots = await load_robots_policy(start_url, settings)
    if not robots.can_fetch(settings.crawler_user_agent, start_url):
        raise CrawlerError("Homepage is disallowed by robots.txt.")

    try:
        return await _crawl_in_browser(url, start_url, robots, settings, audit_id, started_at)
    except SiteBlockedError as exc:
        # Only the homepage's error leaves the browser crawl (an internal page's is recorded in
        # failed_pages). When the site's own security blocked it, the pages may be fetched
        # through Firecrawl instead; otherwise (and with the fallback off) this error stands.
        if site_security_block(exc) is None:
            raise
        blocked = exc
    # Imported here because firecrawl_fallback builds on this module.
    from apps.worker.stages import firecrawl_fallback

    if not firecrawl_fallback.is_enabled(settings):
        raise blocked
    return await firecrawl_fallback.crawl_site_via_firecrawl(
        url,
        start_url=start_url,
        robots=robots,
        settings=settings,
        audit_id=audit_id,
        started_at=started_at,
        blocked=blocked,
    )


async def _crawl_in_browser(
    url: str,
    start_url: str,
    robots: RobotsPolicy,
    settings: Settings,
    audit_id: str | None,
    started_at: str,
) -> CrawlResult:
    """The crawl in our own headless Chromium. A SiteBlockedError leaves it only from the
    homepage: an internal page's failure is recorded in failed_pages and the crawl goes on."""
    failed_pages: list[dict[str, Any]] = []
    skipped_pages: list[dict[str, Any]] = []
    discovered_links: list[LinkCandidate] = []
    pages: list[CrawledPage] = []
    # Shared across every context so the request-level SSRF guard memoizes DNS
    # resolution per host for the whole crawl.
    resolve_cache: dict[str, bool] = {}
    # Cookies from a bot check the homepage passed (SiteGround's _I_, Cloudflare's
    # cf_clearance). Without them every child page's fresh context meets the check again.
    storage_state: Any = None

    async with playwright_api.async_playwright() as playwright:
        browser = await _launch_chromium(playwright, settings)
        try:
            context = await _new_crawl_context(browser, settings, resolve_cache)
            try:
                homepage = await _render_page(context, start_url, settings, audit_id)
                if homepage.passed_interstitial:
                    # Best effort: without the cookies each child page just meets the check.
                    with suppress(playwright_api.Error):
                        storage_state = await context.storage_state()
            finally:
                await context.close()

            if not is_same_site(start_url, homepage.final_url):
                raise CrawlerError("Homepage redirected outside the starting site.")
            # Re-validate the post-redirect host so a redirect to a private/reserved
            # address (or DNS that rebound during navigation) is rejected.
            assert_crawlable_url(homepage.final_url, settings.crawler_allow_private_hosts)

            pages.append(homepage)
            discovered_links = discover_internal_links(homepage.html, homepage.final_url)

            target_candidates = select_child_pages(
                discovered_links,
                robots,
                settings,
                settings.crawler_max_pages,
                homepage.final_url,
                skipped_pages,
            )

            semaphore = asyncio.Semaphore(settings.crawler_concurrency)
            # Set once the site's own bot check blocked a page. Every page not opened yet would
            # meet the same check, and each attempt is one more unsolved check on this server's
            # record with the host, so the rest are skipped instead.
            walled_by: str | None = None

            async def crawl_candidate(candidate: LinkCandidate) -> CrawledPage | None:
                nonlocal walled_by
                async with semaphore:
                    if walled_by is not None:
                        skipped_pages.append(
                            {
                                "url": candidate.url,
                                "status": "skipped",
                                "reason": "stopped_after_bot_check",
                                "source_url": homepage.final_url,
                            }
                        )
                        return None
                    child_context = await _new_crawl_context(
                        browser, settings, resolve_cache, storage_state=storage_state
                    )
                    try:
                        return await _render_page(
                            child_context,
                            candidate.url,
                            settings,
                            audit_id,
                            source_url=homepage.final_url,
                            link_score=candidate.score,
                        )
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as exc:
                        reason = str(exc)
                        if isinstance(exc, SiteBlockedError):
                            # The short reason for this one page: the report prints it next to
                            # the page, so it must not read like the whole audit failed.
                            reason = exc.page_reason
                            walled_by = walled_by or exc.bot_check
                        failed_pages.append(
                            {
                                "url": candidate.url,
                                "status": "failed",
                                "reason": reason,
                                "source_url": homepage.final_url,
                            }
                        )
                        return None
                    finally:
                        await child_context.close()

            crawled_children = await asyncio.gather(
                *(crawl_candidate(candidate) for candidate in target_candidates)
            )
            pages.extend(page for page in crawled_children if page is not None)
        finally:
            await browser.close()

    status = "partial" if failed_pages or skipped_pages else "complete"

    return CrawlResult(
        requested_url=url,
        start_url=start_url,
        final_url=pages[0].final_url,
        status=status,
        pages=pages,
        discovered_links=discovered_links,
        skipped_pages=skipped_pages,
        failed_pages=failed_pages,
        robots=robots,
        started_at=started_at,
        completed_at=_utc_now(),
        max_pages=settings.crawler_max_pages,
        user_agent=settings.crawler_user_agent,
    )


def crawl_site_sync(url: str, settings: Settings, audit_id: str | None = None) -> CrawlResult:
    return asyncio.run(crawl_site(url, settings, audit_id=audit_id))
