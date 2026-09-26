from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
from collections import defaultdict
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
) -> playwright_api.BrowserContext:
    """Create a browser context with the standard crawl options and, unless private
    hosts are allowed, the request-level SSRF guard attached."""
    context = await browser.new_context(
        ignore_https_errors=True,
        service_workers="block",
        user_agent=settings.crawler_user_agent,
        viewport={"width": 1280, "height": 720},
    )
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

    def to_public_dict(self) -> dict[str, Any]:
        return {
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
        }


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=settings.crawler_page_timeout_seconds * 1000,
        )
        with suppress(playwright_api.TimeoutError):
            await page.wait_for_load_state("networkidle", timeout=5000)

        status_code = response.status if response else None
        if is_failed_http_status(status_code):
            raise CrawlerError(f"HTTP {status_code} while rendering {url}")

        html = await page.content()
        title = await page.title()
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
        )
    except playwright_api.TimeoutError as exc:
        raise CrawlerError(f"Timed out rendering {url}") from exc
    except SoftTimeLimitExceeded:
        # Celery's soft limit subclasses Exception: without this re-raise the broad handler
        # below would wrap it into CrawlerError, the honest timed-out failure path in tasks.py
        # would never run, and the worker would run on to the hard-limit SIGKILL.
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


async def crawl_site(url: str, settings: Settings, audit_id: str | None = None) -> CrawlResult:
    started_at = _utc_now()
    start_url = normalize_url(url)
    if start_url is None:
        raise CrawlerError("Audit URL is not a crawlable HTTP/HTTPS URL.")
    assert_crawlable_url(start_url, settings.crawler_allow_private_hosts)

    robots = await load_robots_policy(start_url, settings)
    if not robots.can_fetch(settings.crawler_user_agent, start_url):
        raise CrawlerError("Homepage is disallowed by robots.txt.")

    failed_pages: list[dict[str, Any]] = []
    skipped_pages: list[dict[str, Any]] = []
    discovered_links: list[LinkCandidate] = []
    pages: list[CrawledPage] = []
    # Shared across every context so the request-level SSRF guard memoizes DNS
    # resolution per host for the whole crawl.
    resolve_cache: dict[str, bool] = {}

    async with playwright_api.async_playwright() as playwright:
        browser = await _launch_chromium(playwright, settings)
        try:
            context = await _new_crawl_context(browser, settings, resolve_cache)
            try:
                homepage = await _render_page(context, start_url, settings, audit_id)
            finally:
                await context.close()

            if not is_same_site(start_url, homepage.final_url):
                raise CrawlerError("Homepage redirected outside the starting site.")
            # Re-validate the post-redirect host so a redirect to a private/reserved
            # address (or DNS that rebound during navigation) is rejected.
            assert_crawlable_url(homepage.final_url, settings.crawler_allow_private_hosts)

            pages.append(homepage)
            discovered_links = discover_internal_links(homepage.html, homepage.final_url)

            target_candidates: list[LinkCandidate] = []
            for candidate in discovered_links:
                if len(target_candidates) >= max(settings.crawler_max_pages - 1, 0):
                    break
                if not robots.can_fetch(settings.crawler_user_agent, candidate.url):
                    skipped_pages.append(
                        {
                            "url": candidate.url,
                            "status": "skipped",
                            "reason": "disallowed_by_robots_txt",
                            "source_url": homepage.final_url,
                        }
                    )
                    continue
                target_candidates.append(candidate)

            semaphore = asyncio.Semaphore(settings.crawler_concurrency)

            async def crawl_candidate(candidate: LinkCandidate) -> CrawledPage | None:
                async with semaphore:
                    child_context = await _new_crawl_context(browser, settings, resolve_cache)
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
                        failed_pages.append(
                            {
                                "url": candidate.url,
                                "status": "failed",
                                "reason": str(exc),
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
