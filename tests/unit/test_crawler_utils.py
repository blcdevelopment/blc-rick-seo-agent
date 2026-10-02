import asyncio
import functools
import ipaddress
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

from apps.shared.config import Settings
from apps.worker.stages import crawler
from apps.worker.stages.crawler import (
    BOT_CHECK_BLOCKED_MESSAGE,
    BOT_CHECK_PAGE_REASON,
    INTERSTITIAL_TERMINAL,
    INTERSTITIAL_WAITABLE,
    OTHER_SITE_BOT_CHECK_PAGE_REASON,
    CrawlerError,
    SiteBlockedError,
    assert_crawlable_url,
    discover_internal_links,
    http_error_message,
    http_error_page_reason,
    interstitial_reason,
    is_failed_http_status,
    is_interstitial_url,
    is_same_site,
    normalize_url,
)


def test_normalize_url_removes_fragments_and_default_ports() -> None:
    assert normalize_url("HTTPS://Example.com:443/about/#team") == "https://example.com/about"
    assert normalize_url("/services#top", "https://example.com/") == "https://example.com/services"
    assert normalize_url("mailto:hello@example.com", "https://example.com/") is None
    assert normalize_url("https://user:pass@example.com/") is None
    assert normalize_url("https://example.com:bad/") is None


def test_same_site_allows_www_apex_pair_but_rejects_external_hosts() -> None:
    assert is_same_site("https://example.com", "https://www.example.com/about") is True
    assert is_same_site("https://example.com", "https://other.example.com/about") is False


def test_discover_internal_links_scores_nav_links_and_excludes_external_links() -> None:
    html = """
    <header><nav>
      <a href="/services">Services</a>
      <a href="/contact">Contact</a>
    </nav></header>
    <main class="hero">
      <a href="/services">Our Services</a>
      <a href="https://external.example/">External</a>
    </main>
    <footer><a href="/privacy">Privacy</a></footer>
    """

    links = discover_internal_links(html, "https://www.example.com/")

    assert [link.url for link in links] == [
        "https://www.example.com/services",
        "https://www.example.com/contact",
        "https://www.example.com/privacy",
    ]
    assert "nav" in links[0].sources


def test_private_hosts_are_blocked_by_default() -> None:
    with pytest.raises(CrawlerError):
        assert_crawlable_url("http://127.0.0.1:8000", allow_private_hosts=False)

    assert_crawlable_url("http://127.0.0.1:8000", allow_private_hosts=True)


def test_public_hostname_resolving_to_private_ip_is_blocked(monkeypatch) -> None:
    # DNS-rebinding / metadata SSRF: a public name that resolves to a private IP.
    monkeypatch.setattr(
        crawler, "_resolve_host_ips", lambda hostname: [ipaddress.ip_address("169.254.169.254")]
    )
    with pytest.raises(CrawlerError, match="private"):
        assert_crawlable_url("https://evil.example.com/", allow_private_hosts=False)


def test_public_hostname_resolving_to_public_ip_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(
        crawler, "_resolve_host_ips", lambda hostname: [ipaddress.ip_address("93.184.216.34")]
    )
    assert_crawlable_url("https://example.com/", allow_private_hosts=False)


def test_unresolvable_host_is_blocked(monkeypatch) -> None:
    def _raise(hostname: str):
        raise OSError("Name or service not known")

    monkeypatch.setattr(crawler, "_resolve_host_ips", _raise)
    with pytest.raises(CrawlerError, match="resolve"):
        assert_crawlable_url("https://does-not-exist.example/", allow_private_hosts=False)


def test_failed_http_status_detection() -> None:
    assert is_failed_http_status(None) is False
    assert is_failed_http_status(200) is False
    assert is_failed_http_status(399) is False
    assert is_failed_http_status(400) is True
    assert is_failed_http_status(500) is True


def _crawl_settings(**overrides) -> Settings:
    base = {"crawler_allow_private_hosts": False, "crawler_intercept_requests": True}
    base.update(overrides)
    return Settings(**base)


def test_subrequest_guard_blocks_private_ip_literal() -> None:
    # Cheap literal path: no DNS needed for an IP-literal sub-resource.
    blocked = asyncio.run(crawler._host_blocked_for_subrequest("127.0.0.1", _crawl_settings(), {}))
    assert blocked is True


def test_subrequest_guard_blocks_public_host_resolving_to_metadata_ip(monkeypatch) -> None:
    # Mid-render SSRF: a sub-resource on a public name that resolves to the cloud
    # metadata IP must be aborted (and the decision memoized).
    monkeypatch.setattr(
        crawler, "_resolve_host_ips", lambda hostname: [ipaddress.ip_address("169.254.169.254")]
    )
    cache: dict[str, bool] = {}
    blocked = asyncio.run(
        crawler._host_blocked_for_subrequest("metadata.evil.example", _crawl_settings(), cache)
    )
    assert blocked is True
    assert cache["metadata.evil.example"] is True


def test_subrequest_guard_allows_public_host(monkeypatch) -> None:
    monkeypatch.setattr(
        crawler, "_resolve_host_ips", lambda hostname: [ipaddress.ip_address("93.184.216.34")]
    )
    blocked = asyncio.run(
        crawler._host_blocked_for_subrequest("cdn.example.com", _crawl_settings(), {})
    )
    assert blocked is False


def test_subrequest_guard_disabled_when_private_hosts_allowed() -> None:
    # The QA harness / local crawls set allow_private_hosts; the guard must stand down.
    settings = _crawl_settings(crawler_allow_private_hosts=True)
    blocked = asyncio.run(crawler._host_blocked_for_subrequest("127.0.0.1", settings, {}))
    assert blocked is False


def test_subrequest_guard_disabled_when_interception_off() -> None:
    settings = _crawl_settings(crawler_intercept_requests=False)
    blocked = asyncio.run(crawler._host_blocked_for_subrequest("127.0.0.1", settings, {}))
    assert blocked is False


class _FakeRouteContext:
    """Minimal Playwright context double that records context.route(...) calls."""

    def __init__(self) -> None:
        self.routed: list[str] = []

    def set_default_timeout(self, _ms: int) -> None:
        pass

    def set_default_navigation_timeout(self, _ms: int) -> None:
        pass

    async def route(self, pattern: str, _handler) -> None:
        self.routed.append(pattern)

    async def close(self) -> None:
        pass


class _FakeBrowser:
    """Browser double that hands out _FakeRouteContext and counts new_context calls."""

    def __init__(self, context_cls: type[_FakeRouteContext] = _FakeRouteContext) -> None:
        self.new_context_calls = 0
        self.contexts: list[_FakeRouteContext] = []
        self.context_kwargs: list[dict] = []
        self._context_cls = context_cls

    async def new_context(self, **kwargs) -> _FakeRouteContext:
        self.new_context_calls += 1
        self.context_kwargs.append(kwargs)
        ctx = self._context_cls()
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        pass


def test_new_crawl_context_attaches_ssrf_guard_when_intercepting() -> None:
    browser = _FakeBrowser()
    ctx = asyncio.run(crawler._new_crawl_context(browser, _crawl_settings(), {}))
    assert ctx.routed == ["**/*"]


def test_new_crawl_context_skips_guard_when_private_hosts_allowed() -> None:
    browser = _FakeBrowser()
    ctx = asyncio.run(
        crawler._new_crawl_context(browser, _crawl_settings(crawler_allow_private_hosts=True), {})
    )
    assert ctx.routed == []


def test_new_crawl_context_skips_guard_when_interception_off() -> None:
    browser = _FakeBrowser()
    ctx = asyncio.run(
        crawler._new_crawl_context(browser, _crawl_settings(crawler_intercept_requests=False), {})
    )
    assert ctx.routed == []


def test_crawl_site_builds_every_context_through_the_guarded_helper(monkeypatch) -> None:
    # Regression guard for the request-level SSRF fix: every browser context created during
    # a crawl must come from _new_crawl_context (the single place that attaches the route
    # guard). Previously crawl_site built contexts inline with browser.new_context(...),
    # leaving the interception helper as dead code.
    browser = _FakeBrowser()
    helper_calls = {"count": 0, "storage_states": []}
    real_helper = crawler._new_crawl_context

    async def _spy_helper(b, settings, cache, storage_state=None):
        helper_calls["count"] += 1
        helper_calls["storage_states"].append(storage_state)
        return await real_helper(b, settings, cache, storage_state=storage_state)

    async def _fake_launch(_playwright, _settings):
        return browser

    async def _fake_robots(_url, _settings):
        return SimpleNamespace(can_fetch=lambda *_a, **_k: True)

    async def _fake_render(_context, url, _settings, _audit_id, source_url=None, link_score=None):
        return crawler.CrawledPage(
            url=url,
            final_url=url,
            status_code=200,
            title="Home",
            html="<html><body>No internal links here.</body></html>",
            text="No internal links here.",
            fetched_at="2026-01-01T00:00:00Z",
        )

    class _FakePlaywrightCM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(crawler, "_new_crawl_context", _spy_helper)
    monkeypatch.setattr(crawler, "_launch_chromium", _fake_launch)
    monkeypatch.setattr(crawler, "load_robots_policy", _fake_robots)
    monkeypatch.setattr(crawler, "_render_page", _fake_render)
    monkeypatch.setattr(crawler.playwright_api, "async_playwright", lambda: _FakePlaywrightCM())

    # allow_private_hosts avoids real DNS for the localhost start URL; here we assert that
    # crawl_site delegates context creation to the helper, not whether the guard attaches
    # (that is covered by the tests above).
    settings = _crawl_settings(crawler_allow_private_hosts=True)
    result = asyncio.run(crawler.crawl_site("http://localhost/", settings, "job-1"))

    assert helper_calls["count"] >= 1
    # No context was created outside the helper.
    assert browser.new_context_calls == helper_calls["count"]
    assert len(result.pages) == 1
    # An ordinary site passed no bot check: no cookies are carried, and Playwright gets exactly
    # the standard context options (no storage_state key at all).
    assert helper_calls["storage_states"] == [None] * helper_calls["count"]
    assert all("storage_state" not in kwargs for kwargs in browser.context_kwargs)


class _CookieContext(_FakeRouteContext):
    """A context double that also answers storage_state(), like a Playwright context."""

    STATE = {"cookies": [{"name": "_I_", "value": "token", "domain": ".example.com"}]}

    def __init__(self) -> None:
        super().__init__()
        self.storage_state_calls = 0

    async def storage_state(self) -> dict:
        self.storage_state_calls += 1
        return self.STATE


def test_crawl_site_carries_a_passed_bot_check_cookie_to_child_pages(monkeypatch) -> None:
    # The homepage waited out SiteGround's check: its cookie (_I_) must reach every child
    # context, or each child page meets the check again in its fresh context.
    browser = _FakeBrowser(context_cls=_CookieContext)
    rendered: list[str] = []

    async def _fake_launch(_playwright, _settings):
        return browser

    async def _fake_robots(_url, _settings):
        return SimpleNamespace(can_fetch=lambda *_a, **_k: True)

    async def _fake_render(_context, url, _settings, _audit_id, source_url=None, link_score=None):
        rendered.append(url)
        home = source_url is None
        return crawler.CrawledPage(
            url=url,
            final_url=url,
            status_code=200,
            title="Home" if home else "Child",
            html='<nav><a href="/about">About</a><a href="/contact">Contact</a></nav>',
            text="",
            fetched_at="2026-01-01T00:00:00Z",
            passed_interstitial="SiteGround anti-bot check" if home else None,
        )

    class _FakePlaywrightCM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(crawler, "_launch_chromium", _fake_launch)
    monkeypatch.setattr(crawler, "load_robots_policy", _fake_robots)
    monkeypatch.setattr(crawler, "_render_page", _fake_render)
    monkeypatch.setattr(crawler.playwright_api, "async_playwright", lambda: _FakePlaywrightCM())

    settings = _crawl_settings(crawler_allow_private_hosts=True)
    result = asyncio.run(crawler.crawl_site("http://localhost/", settings, "job-1"))

    assert len(result.pages) == 3
    assert rendered[0] == "http://localhost/"
    assert browser.contexts[0].storage_state_calls == 1
    assert "storage_state" not in browser.context_kwargs[0]
    assert [kwargs.get("storage_state") for kwargs in browser.context_kwargs[1:]] == [
        _CookieContext.STATE,
        _CookieContext.STATE,
    ]


# --- Bot checks ("interstitials"): detection, visitor wording, the bounded wait -----------------

_SG_202_HEADERS = {
    "SG-Captcha": "challenge",
    "X-Robots-Tag": "noindex",
    "Cache-Control": "no-store,no-cache,max-age=0",
}
_SG_CHALLENGE_URL = (
    "https://www.example.com/.well-known/sgcaptcha/?r=%2F&y=ipc:203.0.113.9:1790749657.771"
)


@pytest.mark.parametrize(
    ("url", "status", "headers", "title", "expected"),
    [
        # SiteGround's first answer: 202 + sg-captcha + x-robots-tag noindex + a meta refresh.
        (
            "https://www.example.com/",
            202,
            _SG_202_HEADERS,
            None,
            ("SiteGround anti-bot check", INTERSTITIAL_WAITABLE),
        ),
        # The Robot Challenge Screen itself: its path, header and title.
        (
            _SG_CHALLENGE_URL,
            200,
            {"sg-captcha": "challenge"},
            "Robot Challenge Screen",
            ("SiteGround anti-bot check", INTERSTITIAL_WAITABLE),
        ),
        (
            "https://www.example.com/",
            200,
            {},
            "  Robot Challenge Screen ",
            ("SiteGround anti-bot check", INTERSTITIAL_WAITABLE),
        ),
        (
            "https://www.example.com/.well-known/sgcaptcha/?r=%2F",
            200,
            None,
            None,
            ("SiteGround anti-bot check", INTERSTITIAL_WAITABLE),
        ),
        # SiteGround's human-CAPTCHA fallback keeps the challenge header and title, but only a
        # person can clear it: terminal wins.
        (
            "https://www.example.com/.well-known/captcha/?y=err&r=%2F",
            200,
            {"sg-captcha": "challenge"},
            "Robot Challenge Screen",
            ("SiteGround CAPTCHA", INTERSTITIAL_TERMINAL),
        ),
        # Cloudflare's documented challenge header, on its 403 "Just a moment..." page.
        (
            "https://www.example.com/",
            403,
            {"cf-mitigated": "challenge", "cf-ray": "8c0ffee-IAD", "server": "cloudflare"},
            "Just a moment...",
            ("Cloudflare challenge", INTERSTITIAL_WAITABLE),
        ),
        (
            "https://www.example.com/",
            403,
            {},
            "Just a moment...",
            ("Cloudflare challenge", INTERSTITIAL_WAITABLE),
        ),
        (
            "https://www.example.com/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1",
            200,
            {},
            None,
            ("Cloudflare challenge", INTERSTITIAL_WAITABLE),
        ),
        (
            "https://www.example.com/",
            403,
            {"server": "cloudflare", "cf-ray": "8c0ffee-IAD"},
            "Attention Required! | Cloudflare",
            ("Cloudflare block page", INTERSTITIAL_TERMINAL),
        ),
        # Terminal wins even when a waitable marker is found first (a header before the title).
        (
            "https://www.example.com/",
            403,
            {"cf-mitigated": "challenge"},
            "Attention Required! | Cloudflare",
            ("Cloudflare block page", INTERSTITIAL_TERMINAL),
        ),
        # AWS WAF: challenge (202) can clear itself, CAPTCHA (405) cannot.
        (
            "https://www.example.com/",
            202,
            {"x-amzn-waf-action": "challenge"},
            None,
            ("AWS WAF challenge", INTERSTITIAL_WAITABLE),
        ),
        (
            "https://www.example.com/",
            405,
            {"X-Amzn-Waf-Action": "CAPTCHA"},
            None,
            ("AWS WAF CAPTCHA", INTERSTITIAL_TERMINAL),
        ),
        (
            "https://www.example.com/",
            403,
            {"x-vercel-mitigated": "challenge"},
            None,
            ("Vercel challenge", INTERSTITIAL_WAITABLE),
        ),
    ],
)
def test_interstitial_reason_names_documented_bot_checks(url, status, headers, title, expected):
    assert interstitial_reason(url, status, headers, title) == expected


@pytest.mark.parametrize(
    ("url", "status", "headers", "title"),
    [
        # A normal page behind Cloudflare: cf-ray and "server: cloudflare" are on EVERY proxied
        # response, so they are never a marker on their own.
        (
            "https://www.example.com/",
            200,
            {"cf-ray": "8c0ffee-IAD", "server": "cloudflare", "x-robots-tag": "noindex"},
            "Acme Builders | Custom Homes",
        ),
        # A marker-less 403 (BLC's own site on 30 September): left to the HTTP-status path.
        (
            "https://www.example.com/",
            403,
            {"server": "cloudflare", "cf-ray": "8c0ffee-IAD", "x-proxy-cache-info": "DT:1"},
            "403 - Forbidden",
        ),
        # Exact titles and path prefixes only.
        ("https://www.example.com/", 200, {}, "Just a moment... your quote is loading"),
        ("https://www.example.com/", 200, {}, "Robot Challenge Screen tips"),
        ("https://www.example.com/blog/.well-known/captcha/", 200, {}, "Blog"),
        ("https://www.example.com/well-known/sgcaptcha", 200, {}, "Blog"),
        # Only the documented values count.
        ("https://www.example.com/", 200, {"cf-mitigated": "managed"}, None),
        ("https://www.example.com/", 200, {"sg-captcha": "none"}, None),
        (None, None, None, None),
    ],
)
def test_interstitial_reason_ignores_normal_pages(url, status, headers, title) -> None:
    assert interstitial_reason(url, status, headers, title) is None


def test_is_interstitial_url() -> None:
    assert is_interstitial_url(_SG_CHALLENGE_URL) is True
    assert is_interstitial_url("https://www.example.com/.well-known/captcha/?r=%2F") is True
    assert is_interstitial_url("https://www.example.com/cdn-cgi/challenge-platform/h/b") is True
    assert is_interstitial_url("https://www.example.com/") is False
    assert is_interstitial_url("https://www.example.com/.well-known/security.txt") is False
    assert is_interstitial_url(None) is False


def test_visitor_messages_are_short_and_plain() -> None:
    # The exact text a visitor sees under "Audit failed." (tasks.py stores str(exc)).
    assert BOT_CHECK_BLOCKED_MESSAGE == (
        "We couldn't audit this website. Its security check blocked our scanner, so we stopped "
        "instead of scoring the security screen. If this is your site, ask your web host to "
        "allow our scanner, then try again."
    )
    assert http_error_message(403) == (
        "We couldn't audit this website. It refused our scanner (HTTP 403), usually a firewall "
        "or security setting. If this is your site, ask your web host to allow our scanner, "
        "then try again."
    )
    assert "(HTTP 401)" in http_error_message(401)
    assert http_error_message(429) == (
        "The website asked us to slow down (HTTP 429). Please try again in a few minutes."
    )
    assert http_error_message(404) == (
        "We couldn't find that page (HTTP 404). Please check the address and try again."
    )
    assert http_error_message(410).startswith("We couldn't find that page (HTTP 410).")
    assert http_error_message(500) == (
        "The website returned an error (HTTP 500). Please try again later."
    )
    assert http_error_message(418) == (
        "The website returned an error (HTTP 418). Please try again later."
    )


def test_page_reasons_speak_of_one_page_only() -> None:
    # What the report's "Failed internal pages" table prints next to a page that failed inside
    # an audit that went on: short, and never the wording of a failed audit.
    reasons = {
        403: "Refused by the site (HTTP 403)",
        401: "Refused by the site (HTTP 401)",
        429: "The site asked us to slow down (HTTP 429)",
        404: "Page not found (HTTP 404)",
        410: "Page not found (HTTP 410)",
        500: "The site returned an error (HTTP 500)",
    }
    assert {status: http_error_page_reason(status) for status in reasons} == reasons
    assert BOT_CHECK_PAGE_REASON == "Blocked by the site's security check"
    assert OTHER_SITE_BOT_CHECK_PAGE_REASON == "Leads to another website's security check"
    # A whole-audit failure (the homepage) keeps the visitor text as its page reason too.
    assert SiteBlockedError(BOT_CHECK_BLOCKED_MESSAGE).page_reason == BOT_CHECK_BLOCKED_MESSAGE


class _FakeResponse:
    """A Playwright Response double (sync `headers`, like Response.headers)."""

    def __init__(
        self,
        url: str,
        status: int,
        headers: dict | None = None,
        frame=None,
        navigation: bool = True,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.frame = frame
        self.request = SimpleNamespace(is_navigation_request=lambda: navigation)


class _ChallengePage:
    """A page double for a bot-checked site.

    goto() answers with the check's 202 (reported to the response listener, as Playwright
    does); then each look at the title shows the check, until `clears_after` looks, when the
    browser lands on the real page. `falls_back_after` sends it to SiteGround's human CAPTCHA
    instead, `navigating_looks` makes the first title() calls fail mid-navigation, and
    `navigating_after_clear` does the same right after the real page loaded (a site's own JS
    redirect), with `settle_seconds` spent in each wait_for_load_state(). `first_url` is where
    a redirect lands the first document (another website, say), `body` the page's markup, and
    `breaks` ("crashed" or "closed") makes every title() fail the way a dead page does."""

    BASE = "https://www.example.com"

    def __init__(
        self,
        *,
        clears_after: int | None = None,
        falls_back_after: int | None = None,
        navigating_looks: int = 0,
        navigating_after_clear: int = 0,
        settle_seconds: float = 0.0,
        first_status: int = 202,
        first_headers: dict | None = None,
        first_title: str = "",
        first_url: str | None = None,
        real_status: int = 200,
        body: str = "<h1>Home</h1>",
        breaks: str | None = None,
    ) -> None:
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.url = "about:blank"
        self._title = ""
        self._listeners: dict[str, list] = {"response": [], "crash": []}
        self._clears_after = clears_after
        self._falls_back_after = falls_back_after
        self._navigating_looks = navigating_looks
        self._navigating_after_clear = navigating_after_clear
        self._settle_seconds = settle_seconds
        self._first = (
            first_status,
            _SG_202_HEADERS if first_headers is None else first_headers,
            first_title,
        )
        self._first_url = first_url
        self._real_status = real_status
        self._body = body
        self._breaks = breaks
        self.looks = 0
        self.closed = False

    def on(self, event: str, callback) -> None:
        self._listeners[event].append(callback)

    def _emit(self, response: _FakeResponse) -> None:
        for callback in self._listeners["response"]:
            callback(response)

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **_kwargs) -> _FakeResponse:
        status, headers, title = self._first
        landed = self._first_url or url
        if landed != url:  # the redirect's own 3xx reaches the listener too
            self._emit(_FakeResponse(url, 302, {"location": landed}, frame=self.main_frame))
        self.url, self._title = landed, title
        first = _FakeResponse(landed, status, headers, frame=self.main_frame)
        self._emit(first)
        if status == 202:  # SiteGround: the meta refresh lands on the Robot Challenge Screen
            self.url, self._title = _SG_CHALLENGE_URL, "Robot Challenge Screen"
            self._emit(
                _FakeResponse(self.url, 200, {"sg-captcha": "challenge"}, frame=self.main_frame)
            )
        return first

    async def wait_for_load_state(self, _state: str, timeout=None) -> None:
        await asyncio.sleep(self._settle_seconds)

    async def title(self) -> str:
        self.looks += 1
        if self._breaks == "crashed":
            for callback in self._listeners["crash"]:
                callback(self)
            raise crawler.playwright_api.Error("Target crashed")
        if self._breaks == "closed":
            self.closed = True
            raise crawler.playwright_api.Error("Target page, context or browser has been closed")
        if self.looks <= self._navigating_looks:
            raise crawler.playwright_api.Error("Execution context was destroyed")
        cleared_at = None if self._clears_after is None else self._clears_after + 1
        if cleared_at is not None and cleared_at < self.looks <= (
            cleared_at + self._navigating_after_clear
        ):
            raise crawler.playwright_api.Error("Execution context was destroyed")
        if self._clears_after is not None and self.looks == self._clears_after + 1:
            self._emit(
                _FakeResponse(
                    f"{self.BASE}/.well-known/sgcaptcha/?sol=1", 302, frame=self.main_frame
                )
            )
            self.url, self._title = f"{self.BASE}/", "Acme Builders | Home"
            self._emit(_FakeResponse(self.url, self._real_status, {}, frame=self.main_frame))
        if self._falls_back_after is not None and self.looks == self._falls_back_after + 1:
            self.url = f"{self.BASE}/.well-known/captcha/?y=err&r=%2F"
            self._emit(
                _FakeResponse(self.url, 200, {"sg-captcha": "challenge"}, frame=self.main_frame)
            )
        return self._title

    async def content(self) -> str:
        return f"<html><head><title>{self._title}</title></head><body>{self._body}</body></html>"

    def locator(self, _selector: str):
        page = self

        class _Body:
            async def inner_text(self, timeout=None) -> str:
                return f"{page._title} Home"

        return _Body()

    async def close(self) -> None:
        self.closed = True


class _PageContext:
    def __init__(self, page: _ChallengePage) -> None:
        self.page = page

    async def new_page(self) -> _ChallengePage:
        return self.page


def _wait_settings(seconds: float) -> SimpleNamespace:
    return SimpleNamespace(
        crawler_challenge_wait_seconds=seconds,
        crawler_page_timeout_seconds=30,
        crawler_screenshots_enabled=False,
        accessibility_advisory_enabled=False,
    )


@pytest.fixture
def fast_polls(monkeypatch):
    monkeypatch.setattr(crawler, "_INTERSTITIAL_POLL_SECONDS", 0.001)


def _wait(page: _ChallengePage, seconds: float):
    async def run():
        documents = crawler._DocumentWatch(page)
        page.on("response", documents.on_response)
        page.on("crash", documents.on_crash)
        url = f"{page.BASE}/"
        response = await page.goto(url)
        return await crawler._wait_out_interstitial(
            page, url, response, documents, _wait_settings(seconds)
        )

    return asyncio.run(run())


def test_wait_returns_the_real_page_once_the_site_check_passes(fast_polls) -> None:
    page = _ChallengePage(clears_after=3)
    response, passed = _wait(page, seconds=5)
    # The real document, not goto's 202, and the label of the check that was waited out.
    assert response.status == 200
    assert response.url == f"{page.BASE}/"
    assert passed == "SiteGround anti-bot check"


def test_wait_counts_a_check_passed_before_the_first_look(fast_polls) -> None:
    # A fast CPU passes the check inside the networkidle wait: the first look already shows
    # the real page, and only the response listener saw the check (so cookies get carried).
    page = _ChallengePage(clears_after=0)
    response, passed = _wait(page, seconds=5)
    assert response.status == 200
    assert passed == "SiteGround anti-bot check"
    # No polling: the first look is already the real page. It gets one more look once it has
    # settled, as after any passed check (the networkidle wait may have ended mid-load).
    assert page.looks == 2


def test_wait_gives_up_with_the_plain_message_when_the_check_stays(fast_polls) -> None:
    page = _ChallengePage()
    started = time.monotonic()
    with pytest.raises(SiteBlockedError) as excinfo:
        _wait(page, seconds=0.2)
    assert time.monotonic() - started >= 0.2
    assert str(excinfo.value) == BOT_CHECK_BLOCKED_MESSAGE


def test_wait_stops_at_once_on_a_check_only_a_person_can_clear(fast_polls) -> None:
    page = _ChallengePage(falls_back_after=2)
    started = time.monotonic()
    with pytest.raises(SiteBlockedError):
        _wait(page, seconds=30)
    assert time.monotonic() - started < 5
    assert page.looks == 3


def test_zero_wait_budget_fails_at_once_on_a_check(fast_polls) -> None:
    with pytest.raises(SiteBlockedError):
        _wait(_ChallengePage(clears_after=3), seconds=0)


def test_wait_leaves_ordinary_pages_alone(fast_polls) -> None:
    page = _ChallengePage(first_status=200, first_headers={}, first_title="Acme Builders")
    response, passed = _wait(page, seconds=5)
    assert (response.status, passed, page.looks) == (200, None, 1)


def test_a_passed_check_is_not_called_blocked_when_the_page_keeps_navigating(fast_polls) -> None:
    # The check let the browser through, then the real page ran its own redirect past the
    # budget: the check is no longer in the way, so the page is captured, not reported blocked.
    page = _ChallengePage(clears_after=1, navigating_after_clear=50, settle_seconds=0.03)
    response, passed = _wait(page, seconds=0.05)
    assert response.status == 200
    assert passed == "SiteGround anti-bot check"


def test_wait_rides_out_a_navigating_page_without_calling_it_a_check(fast_polls) -> None:
    # title() fails while an ordinary page is still navigating: that is no bot check, and no
    # page to capture yet either. Two failed looks, one that works, one after it settled.
    page = _ChallengePage(
        first_status=200, first_headers={}, first_title="Acme Builders", navigating_looks=2
    )
    response, passed = _wait(page, seconds=5)
    assert (response.status, passed) == (200, None)
    assert page.looks == 4


def test_a_check_page_mid_navigation_is_never_captured(fast_polls) -> None:
    # title() fails at the first looks while SiteGround's meta refresh and solve redirect replace
    # the document: the check is still running, so nothing is captured until the real page.
    page = _ChallengePage(clears_after=3, navigating_looks=2)
    crawled = _render(page, seconds=5)
    assert crawled.title == "Acme Builders | Home"
    assert crawled.final_url == f"{page.BASE}/"
    assert crawled.passed_interstitial == "SiteGround anti-bot check"


def test_document_watch_keeps_only_the_main_frames_final_documents() -> None:
    # The page's status and headers come from `latest`, so only the main frame's own documents
    # may set it, and no redirect. Cloudflare's traps sit under its challenge path prefix: the
    # JS-detection script on every page it proxies (a sub-resource) and the Turnstile widget
    # (a child frame's document).
    page = SimpleNamespace(main_frame=object())
    documents = crawler._DocumentWatch(page)
    documents.on_response(
        _FakeResponse(
            "https://www.example.com/cdn-cgi/challenge-platform/scripts/jsd/main.js",
            200,
            frame=page.main_frame,
            navigation=False,
        )
    )
    documents.on_response(
        _FakeResponse(
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2",
            200,
            frame=object(),
        )
    )
    documents.on_response(
        _FakeResponse(
            "https://www.example.com/.well-known/sgcaptcha/?sol=1",
            302,
            {"sg-captcha": "challenge"},
            frame=page.main_frame,
        )
    )
    assert (documents.latest, documents.seen) == (None, None)

    home = _FakeResponse("https://www.example.com/", 200, frame=page.main_frame)
    documents.on_response(home)
    assert (documents.latest, documents.seen) == (home, None)


def _render(page: _ChallengePage, seconds: float):
    return asyncio.run(
        crawler._render_page(_PageContext(page), f"{page.BASE}/", _wait_settings(seconds), "job-1")
    )


def test_render_page_records_the_real_page_after_a_passed_check(fast_polls) -> None:
    page = _ChallengePage(clears_after=2)
    crawled = _render(page, seconds=5)
    assert crawled.status_code == 200  # the real document's status, not the check's 202
    assert crawled.final_url == f"{page.BASE}/"
    assert crawled.title == "Acme Builders | Home"
    assert crawled.passed_interstitial == "SiteGround anti-bot check"
    assert crawled.to_public_dict()["passed_interstitial"] == "SiteGround anti-bot check"
    assert page.closed is True


def test_render_page_blocked_by_a_check_raises_the_plain_text_unwrapped(fast_polls) -> None:
    page = _ChallengePage()
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=0.05)
    assert str(excinfo.value) == BOT_CHECK_BLOCKED_MESSAGE
    assert "Could not render" not in str(excinfo.value)
    assert page.closed is True


def test_render_page_http_error_raises_the_plain_text_unwrapped(fast_polls) -> None:
    # A plain 403 (no bot-check marker): no waiting, and the visitor sees the plain text, not
    # "Could not render <url>: HTTP 403 while rendering <url>".
    page = _ChallengePage(
        first_status=403,
        first_headers={"server": "cloudflare", "cf-ray": "8c0ffee-IAD"},
        first_title="403 - Forbidden",
    )
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=15)
    assert str(excinfo.value) == http_error_message(403)
    assert page.looks == 1


@pytest.mark.parametrize("status", [401, 403])
def test_only_the_audited_sites_own_refusal_records_its_status(fast_polls, status) -> None:
    # A refusal the site itself answered is its security (the Firecrawl fallback may help); one a
    # redirect to ANOTHER website answered is not, whatever the visitor's text says.
    own = _ChallengePage(first_status=status, first_headers={}, first_title="Forbidden")
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(own, seconds=0)
    assert excinfo.value.status_code == status
    assert crawler.site_security_block(excinfo.value) == f"HTTP {status}"

    elsewhere = _ChallengePage(
        first_status=status,
        first_headers={},
        first_title="Forbidden",
        first_url="https://login.saas.example/sso",
    )
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(elsewhere, seconds=0)
    assert str(excinfo.value) == http_error_message(status)
    assert excinfo.value.status_code is None
    assert crawler.site_security_block(excinfo.value) is None


def test_render_page_error_after_a_passed_check_uses_the_final_status(fast_polls) -> None:
    page = _ChallengePage(clears_after=1, real_status=404)
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=5)
    assert str(excinfo.value) == http_error_message(404)
    assert excinfo.value.page_reason == "Page not found (HTTP 404)"
    assert excinfo.value.bot_check is None


def test_render_page_waits_out_a_cloudflare_challenge_that_answers_403(fast_polls) -> None:
    # Cloudflare's managed challenge answers 403 first. Once it clears, the page records the
    # real document's status: not the challenge's 403 ("It refused our scanner").
    page = _ChallengePage(
        first_status=403,
        first_headers={"cf-mitigated": "challenge"},
        first_title="Just a moment...",
        clears_after=2,
    )
    crawled = _render(page, seconds=5)
    assert crawled.status_code == 200
    assert crawled.passed_interstitial == "Cloudflare challenge"


class _PageWithBrokenEmbed(_ChallengePage):
    """An ordinary page whose embedded iframe (another frame) loads a document answering 404,
    as a broken map or form embed does."""

    async def goto(self, url: str, **kwargs) -> _FakeResponse:
        first = await super().goto(url, **kwargs)
        self._emit(_FakeResponse(f"{self.BASE}/embed/map", 404, {}, frame=object()))
        return first


def test_a_child_frame_document_never_sets_the_page_status(fast_polls) -> None:
    page = _PageWithBrokenEmbed(first_status=200, first_headers={}, first_title="Acme Builders")
    crawled = _render(page, seconds=5)
    assert crawled.status_code == 200


def test_a_check_on_another_website_fails_that_page_at_once(fast_polls) -> None:
    # An internal link that redirects to a third party's portal behind Cloudflare. That site is
    # not being audited: nobody waits for its check, and it walls nothing (bot_check is None).
    page = _ChallengePage(
        first_url="https://portal.saas.example/login",
        first_status=403,
        first_headers={"cf-mitigated": "challenge"},
        first_title="Just a moment...",
    )
    started = time.monotonic()
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=30)
    assert time.monotonic() - started < 5
    assert page.looks == 1
    assert excinfo.value.page_reason == OTHER_SITE_BOT_CHECK_PAGE_REASON
    assert excinfo.value.bot_check is None


class _PassesAnotherWebsitesCheckMidNavigation(_ChallengePage):
    """Lands on another website's check, whose next document is that site's real page, while
    title() keeps failing (a navigation that has not settled when the wait ends)."""

    async def goto(self, url: str, **kwargs) -> _FakeResponse:
        first = await super().goto(url, **kwargs)
        self._emit(_FakeResponse(self.url, 200, {}, frame=self.main_frame))
        return first


def test_the_wait_ending_on_another_website_walls_nothing(fast_polls) -> None:
    # A check was seen, but on another website: when the budget ends mid-navigation there, the
    # page fails as a link to another website's check, never as the audited site's own wall.
    page = _PassesAnotherWebsitesCheckMidNavigation(
        first_url="https://portal.saas.example/login",
        first_status=403,
        first_headers={"cf-mitigated": "challenge"},
        first_title="Just a moment...",
        navigating_looks=99,
    )
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=0)
    assert excinfo.value.page_reason == OTHER_SITE_BOT_CHECK_PAGE_REASON
    assert excinfo.value.bot_check is None


def test_a_navigation_into_a_check_is_the_check_not_just_navigating() -> None:
    # title() fails mid-navigation, but the document the browser is moving to is SiteGround's
    # check (its address and header say so): the check is in the way, so a wait that ends now
    # reports it instead of capturing whatever the page shows.
    page = _ChallengePage(navigating_looks=1)
    documents = crawler._DocumentWatch(page)
    documents.latest = _FakeResponse(
        _SG_CHALLENGE_URL, 200, {"sg-captcha": "challenge"}, frame=page.main_frame
    )
    found = asyncio.run(crawler._interstitial_now(page, documents, None))
    assert found == ("SiteGround anti-bot check", INTERSTITIAL_WAITABLE)


@pytest.mark.parametrize("breaks", ["crashed", "closed"])
def test_a_dead_page_fails_at_once_not_after_the_wait(fast_polls, breaks) -> None:
    # title() fails on a crashed or closed page as well, but that page is not navigating:
    # waiting out the budget would only delay the failure, and call it the site's check.
    page = _ChallengePage(breaks=breaks)
    started = time.monotonic()
    with pytest.raises(CrawlerError) as excinfo:
        _render(page, seconds=30)
    assert time.monotonic() - started < 5
    assert page.looks == 1
    assert not isinstance(excinfo.value, SiteBlockedError)


def test_a_check_only_its_title_gives_away_is_never_scored(fast_polls) -> None:
    # The wait ends with the page still navigating and no check seen, so the page is captured;
    # its title then shows Cloudflare's "Just a moment..." screen, which is never scored.
    page = _ChallengePage(
        first_status=200, first_headers={}, first_title="Just a moment...", navigating_looks=1
    )
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=0)
    assert excinfo.value.page_reason == BOT_CHECK_PAGE_REASON
    assert excinfo.value.bot_check == "Cloudflare challenge"


# --- A bot check or an HTTP error on an internal page (the homepage loaded) ----------------------


class _RoutedPage:
    """Picks its _ChallengePage at goto(), once the address is known, and hands it the
    listeners registered before (_render_page registers them before goto)."""

    def __init__(self, site: dict[str, dict], opened: list[str]) -> None:
        self._site = site
        self._opened = opened
        self._listeners: list = []
        self._page: _ChallengePage | None = None

    def on(self, event: str, callback) -> None:
        self._listeners.append((event, callback))

    async def goto(self, url: str, **kwargs) -> _FakeResponse:
        self._opened.append(url)
        path = urlparse(url).path
        ordinary = {"first_status": 200, "first_headers": {}, "first_title": path}
        self._page = _ChallengePage(**self._site.get(path, ordinary))
        for event, callback in self._listeners:
            self._page.on(event, callback)
        return await self._page.goto(url, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._page, name)


class _SiteContext(_FakeRouteContext):
    """A context whose pages answer like a small website: `site` maps a path to the
    _ChallengePage options it shows (an ordinary page when the path is not listed)."""

    def __init__(self, site: dict[str, dict], opened: list[str]) -> None:
        super().__init__()
        self._site = site
        self._opened = opened

    async def new_page(self) -> _RoutedPage:
        return _RoutedPage(self._site, self._opened)


def _home_linking(*paths: str) -> dict:
    links = "".join(f'<a href="{path}">{path}</a>' for path in paths)
    return {
        "first_status": 200,
        "first_headers": {},
        "first_title": "Acme Builders | Home",
        "body": f"<nav>{links}</nav>",
    }


def _crawl_site_like(monkeypatch, site: dict[str, dict], **overrides):
    """Run the real crawl_site and _render_page over `site`; return the result, the addresses
    the browser opened (in order) and the browser double."""
    opened: list[str] = []
    browser = _FakeBrowser(context_cls=functools.partial(_SiteContext, site, opened))

    async def _fake_launch(_playwright, _settings):
        return browser

    async def _fake_robots(_url, _settings):
        return SimpleNamespace(can_fetch=lambda *_a, **_k: True)

    class _FakePlaywrightCM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(crawler, "_launch_chromium", _fake_launch)
    monkeypatch.setattr(crawler, "load_robots_policy", _fake_robots)
    monkeypatch.setattr(crawler.playwright_api, "async_playwright", lambda: _FakePlaywrightCM())
    settings = _crawl_settings(
        crawler_allow_private_hosts=True,
        crawler_concurrency=1,
        crawler_screenshots_enabled=False,
        **overrides,
    )
    result = asyncio.run(crawler.crawl_site("https://www.example.com/", settings, "job-1"))
    return result, opened, browser


def test_failed_internal_pages_get_a_short_reason_of_their_own(monkeypatch, fast_polls) -> None:
    # The report prints each failed page's reason in its "Failed internal pages" table, inside a
    # report that WAS built: the reason speaks of that page, never "We couldn't audit this
    # website" or "Please check the address" (the visitor typed neither address).
    site = {
        "/": _home_linking("/old-page", "/client-login", "/about"),
        "/old-page": {"first_status": 404, "first_headers": {}, "first_title": "Not found"},
        "/client-login": {"first_status": 403, "first_headers": {}, "first_title": "Forbidden"},
    }
    result, _opened, _browser = _crawl_site_like(monkeypatch, site)

    assert {page["url"]: page["reason"] for page in result.failed_pages} == {
        "https://www.example.com/old-page": "Page not found (HTTP 404)",
        "https://www.example.com/client-login": "Refused by the site (HTTP 403)",
    }
    assert [page.final_url for page in result.pages] == [
        "https://www.example.com/",
        "https://www.example.com/about",
    ]
    assert result.skipped_pages == []


def test_the_sites_own_check_on_a_page_stops_the_crawl_there(monkeypatch, fast_polls) -> None:
    # Every page not opened yet would meet the same check, one more unsolved check each on the
    # server's record: after the first blocked page the rest are skipped, never opened.
    site = {"/": _home_linking("/a", "/b", "/c"), "/a": {}, "/b": {}, "/c": {}}
    result, opened, browser = _crawl_site_like(monkeypatch, site, crawler_challenge_wait_seconds=0)

    assert len(opened) == 2  # the homepage and ONE of the checked pages
    assert len(browser.contexts) == 2
    assert [page["reason"] for page in result.failed_pages] == [BOT_CHECK_PAGE_REASON]
    assert [page["reason"] for page in result.skipped_pages] == ["stopped_after_bot_check"] * 2
    assert {page["url"] for page in result.failed_pages + result.skipped_pages} == {
        "https://www.example.com/a",
        "https://www.example.com/b",
        "https://www.example.com/c",
    }
    assert result.status == "partial"


def test_a_link_to_another_websites_check_does_not_stop_the_crawl(monkeypatch, fast_polls) -> None:
    site = {
        "/": _home_linking("/client-portal", "/about", "/contact"),
        "/client-portal": {
            "first_url": "https://portal.saas.example/login",
            "first_status": 403,
            "first_headers": {"cf-mitigated": "challenge"},
            "first_title": "Just a moment...",
        },
    }
    started = time.monotonic()
    result, opened, _browser = _crawl_site_like(
        monkeypatch, site, crawler_challenge_wait_seconds=30
    )

    assert time.monotonic() - started < 5  # nobody waits for another website's check
    assert len(opened) == 4
    assert [(page["url"], page["reason"]) for page in result.failed_pages] == [
        ("https://www.example.com/client-portal", OTHER_SITE_BOT_CHECK_PAGE_REASON)
    ]
    assert result.skipped_pages == []
    assert len(result.pages) == 3


def test_robots_txt_answered_by_a_bot_check_is_unavailable_not_loaded(monkeypatch) -> None:
    # SiteGround's 202 challenge HTML parses to zero robots rules; it must not be reported as a
    # loaded robots.txt that allows everything.
    real_client = httpx.AsyncClient

    def _client(handler):
        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        return factory

    def challenged(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, headers=_SG_202_HEADERS, text="<html></html>", request=request)

    def served(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", request=request)

    monkeypatch.setattr(crawler.httpx, "AsyncClient", _client(challenged))
    policy = asyncio.run(crawler.load_robots_policy("https://www.example.com/", _crawl_settings()))
    assert policy.status == "unavailable"
    assert "bot check" in (policy.error or "")
    assert policy.can_fetch("BLC-Audit-Bot/1.0", "https://www.example.com/private") is True

    monkeypatch.setattr(crawler.httpx, "AsyncClient", _client(served))
    policy = asyncio.run(crawler.load_robots_policy("https://www.example.com/", _crawl_settings()))
    assert policy.status == "loaded"
    assert policy.can_fetch("BLC-Audit-Bot/1.0", "https://www.example.com/private") is False


class _StubFrameScanPage:
    def __init__(self) -> None:
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.evaluate_calls = 0

    async def evaluate(self, script):
        self.evaluate_calls += 1
        return None

    async def wait_for_load_state(self, state, timeout=None):
        return None


def test_frame_scan_skips_pages_with_no_embed_signal() -> None:
    # No child frame, no iframe markup, no provider loader in the static HTML: the scan must
    # bail out BEFORE the ~1-3s scroll nudge — an iframe-less site should not pay it per page.
    page = _StubFrameScanPage()
    html = "<html><body><h1>Plain site</h1><form><input name='email'></form></body></html>"
    forms, fields = asyncio.run(crawler._scan_frames_for_forms(page, html))
    assert (forms, fields) == (0, 0)
    assert page.evaluate_calls == 0


def test_frame_scan_runs_when_provider_signature_present() -> None:
    # A popup/JS-mounted embed leaves its loader script in the static HTML even though the
    # <iframe> only mounts later — the signature must keep the scan alive.
    page = _StubFrameScanPage()
    html = "<script src='https://link.msgsndr.com/js/form_embed.js'></script>"
    forms, fields = asyncio.run(crawler._scan_frames_for_forms(page, html))
    assert (forms, fields) == (0, 0)  # no child frames mounted in this stub
    assert page.evaluate_calls >= 1  # but the nudge ran


def test_frame_scan_reraises_soft_time_limit() -> None:
    # The worker's soft time limit must propagate (task marks the job failed honestly) —
    # a bare except here would strand the job until the hard limit SIGKILLs the worker.
    from celery.exceptions import SoftTimeLimitExceeded

    class _TimedOutPage(_StubFrameScanPage):
        async def evaluate(self, script):
            raise SoftTimeLimitExceeded()

    page = _TimedOutPage()
    html = "<iframe src='https://form.jotform.com/x'></iframe>"
    with pytest.raises(SoftTimeLimitExceeded):
        asyncio.run(crawler._scan_frames_for_forms(page, html))
