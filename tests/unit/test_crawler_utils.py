import asyncio
import ipaddress
from types import SimpleNamespace

import pytest

from apps.shared.config import Settings
from apps.worker.stages import crawler
from apps.worker.stages.crawler import (
    CrawlerError,
    assert_crawlable_url,
    discover_internal_links,
    is_failed_http_status,
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

    def __init__(self) -> None:
        self.new_context_calls = 0
        self.contexts: list[_FakeRouteContext] = []

    async def new_context(self, **_kwargs) -> _FakeRouteContext:
        self.new_context_calls += 1
        ctx = _FakeRouteContext()
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
    helper_calls = {"count": 0}
    real_helper = crawler._new_crawl_context

    async def _spy_helper(b, settings, cache):
        helper_calls["count"] += 1
        return await real_helper(b, settings, cache)

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
