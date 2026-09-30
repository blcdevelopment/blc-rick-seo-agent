import asyncio
import ipaddress
import time
from types import SimpleNamespace

import httpx
import pytest

from apps.shared.config import Settings
from apps.worker.stages import crawler
from apps.worker.stages.crawler import (
    BOT_CHECK_BLOCKED_MESSAGE,
    INTERSTITIAL_TERMINAL,
    INTERSTITIAL_WAITABLE,
    CrawlerError,
    SiteBlockedError,
    assert_crawlable_url,
    discover_internal_links,
    http_error_message,
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


class _FakeResponse:
    """A Playwright Response double (sync `headers`, like Response.headers)."""

    def __init__(self, url: str, status: int, headers: dict | None = None, frame=None) -> None:
        self.url = url
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.frame = frame
        self.request = SimpleNamespace(is_navigation_request=lambda: True)


class _ChallengePage:
    """A page double for a bot-checked site.

    goto() answers with the check's 202 (reported to the response listener, as Playwright
    does); then each look at the title shows the check, until `clears_after` looks, when the
    browser lands on the real page. `falls_back_after` sends it to SiteGround's human CAPTCHA
    instead, `navigating_looks` makes the first title() calls fail mid-navigation, and
    `navigating_after_clear` does the same right after the real page loaded (a site's own JS
    redirect), with `settle_seconds` spent in each wait_for_load_state()."""

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
        real_status: int = 200,
    ) -> None:
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.url = "about:blank"
        self._title = ""
        self._listeners: list = []
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
        self._real_status = real_status
        self.looks = 0
        self.closed = False

    def on(self, event: str, callback) -> None:
        assert event == "response"
        self._listeners.append(callback)

    def _emit(self, response: _FakeResponse) -> None:
        for callback in self._listeners:
            callback(response)

    async def goto(self, url: str, **_kwargs) -> _FakeResponse:
        status, headers, title = self._first
        self.url, self._title = url, title
        first = _FakeResponse(url, status, headers, frame=self.main_frame)
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
        if self.looks <= self._navigating_looks:
            raise crawler.playwright_api.Error("Execution context was destroyed")
        cleared_at = None if self._clears_after is None else self._clears_after + 1
        if cleared_at is not None and cleared_at < self.looks <= (
            cleared_at + self._navigating_after_clear
        ):
            raise crawler.playwright_api.Error("Execution context was destroyed")
        if self._clears_after is not None and self.looks == self._clears_after + 1:
            self._emit(_FakeResponse(f"{self.BASE}/.well-known/sgcaptcha/?sol=1", 302))
            self.url, self._title = f"{self.BASE}/", "Acme Builders | Home"
            self._emit(_FakeResponse(self.url, self._real_status, {}, frame=self.main_frame))
        if self._falls_back_after is not None and self.looks == self._falls_back_after + 1:
            self.url = f"{self.BASE}/.well-known/captcha/?y=err&r=%2F"
            self._emit(
                _FakeResponse(self.url, 200, {"sg-captcha": "challenge"}, frame=self.main_frame)
            )
        return self._title

    async def content(self) -> str:
        return f"<html><head><title>{self._title}</title></head><body><h1>Home</h1></body></html>"

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
        response = await page.goto(f"{page.BASE}/")
        return await crawler._wait_out_interstitial(
            page, response, documents, _wait_settings(seconds)
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
    assert page.looks == 1  # no waiting at all: the first look is already the real page


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
    # title() fails while an ordinary page is still navigating: that is no bot check.
    page = _ChallengePage(
        first_status=200, first_headers={}, first_title="Acme Builders", navigating_looks=2
    )
    response, passed = _wait(page, seconds=5)
    assert (response.status, passed) == (200, None)


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


def test_render_page_error_after_a_passed_check_uses_the_final_status(fast_polls) -> None:
    page = _ChallengePage(clears_after=1, real_status=404)
    with pytest.raises(SiteBlockedError) as excinfo:
        _render(page, seconds=5)
    assert str(excinfo.value) == http_error_message(404)


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
