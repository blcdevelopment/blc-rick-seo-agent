from __future__ import annotations

import asyncio
import functools
import http.server
import ipaddress
import socketserver
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from apps.worker.stages import site_health
from apps.worker.stages.external_seo import collect_external_seo_facts
from apps.worker.stages.site_health import collect_site_health_facts


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "site_health_enabled": True,
        "site_health_max_internal_urls": 150,
        "site_health_max_external_urls": 50,
        "site_health_check_external_links": False,
        "site_health_concurrency": 4,
        "site_health_per_host_concurrency": 2,
        # Tests run against a local fixture server; politeness pacing would only slow them.
        "site_health_request_delay_ms": 0,
        "site_health_breaker_threshold": 5,
        "site_health_request_timeout_seconds": 5,
        "site_health_total_budget_seconds": 30,
        "site_health_sitemap_max_urls": 100,
        "crawler_user_agent": "test-agent",
        "crawler_allow_private_hosts": True,
        "screaming_frog_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/ok": (200, b"ok"),
        "/missing": (404, b"not found"),
        "/error": (500, b"boom"),
        "/sitemap.xml": (
            200,
            b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>http://{host}/from-sitemap</loc></url></urlset>",
        ),
        "/from-sitemap": (200, b"sitemap page"),
        "/noindexed": (200, b"hidden"),
    }

    def _respond(self, include_body: bool) -> None:
        if "drop" in self.path:
            # Not a valid HTTP response: the checker sees a protocol-level transport
            # failure (counts toward the breaker) without the server raising.
            self.close_connection = True
            self.wfile.write(b"garbage\r\n\r\n")
            return
        status, body = self.routes.get(self.path, (404, b"unknown"))
        body = body.replace(b"{host}", self.headers.get("Host", "").encode())
        self.send_response(status)
        if self.path == "/noindexed":
            self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._respond(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._respond(include_body=False)

    def log_message(self, *args) -> None:
        return


@contextmanager
def _serve(
    handler_cls: type[http.server.BaseHTTPRequestHandler] = _Handler,
) -> Iterator[str]:
    handler = functools.partial(handler_cls)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _seo_facts_with_page_problems() -> dict:
    page = {
        "url": "https://example.com/",
        "title": {"text": "Same Title"},
        "meta_description": {"text": None},
        "headings": {"counts": {"h1": 0}},
        "canonical": None,
        "images": {"missing_alt": 3},
        "robots": {"noindex": True},
    }
    twin = {
        **page,
        "url": "https://example.com/twin",
        "robots": {"noindex": False},
        "images": {"missing_alt": 0},
    }
    return {"status": "complete", "pages": [page, twin]}


def test_disabled_sweep_returns_skipped() -> None:
    facts = collect_site_health_facts(
        url="https://example.com/",
        seo_facts={},
        crawled_pages={},
        rendered_pages=None,
        settings=_settings(site_health_enabled=False),
    )
    assert facts["status"] == "skipped"
    assert facts["reason"] == "disabled"


def test_on_page_checks_count_with_examples() -> None:
    facts = collect_site_health_facts(
        url="https://example.com/",
        seo_facts=_seo_facts_with_page_problems(),
        crawled_pages={"final_url": "https://example.com/"},
        rendered_pages=None,
        settings=_settings(site_health_sitemap_max_urls=0),
    )

    assert facts["status"] == "complete"
    summary = facts["summary"]
    assert summary["duplicate_titles"] == 2
    assert summary["missing_meta_descriptions"] == 2
    assert summary["missing_h1"] == 2
    assert summary["missing_canonicals"] == 2
    assert summary["images_missing_alt"] == 3
    assert summary["non_indexable_internal_urls"] == 1
    issues = {issue["id"]: issue for issue in facts["issues"]}
    assert "https://example.com/" in issues["duplicate_titles"]["examples"]
    assert "https://example.com/" in issues["images_missing_alt"]["examples"]


def test_link_sweep_records_status_codes_and_sitemap_urls() -> None:
    with _serve() as base:
        crawled_pages = {
            "final_url": f"{base}/",
            "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
            "discovered_links": [
                {"url": f"{base}/ok"},
                {"url": f"{base}/missing"},
                {"url": f"{base}/error"},
                {"url": f"{base}/noindexed"},
            ],
        }
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages=crawled_pages,
            rendered_pages=None,
            settings=_settings(),
        )

    assert facts["status"] == "complete"
    summary = facts["summary"]
    assert summary["client_error_internal_urls"] == 1
    assert summary["server_error_internal_urls"] == 1
    assert summary["non_indexable_internal_urls"] == 1
    # /ok, /missing, /error, /noindexed and the sitemap-discovered page; the
    # rendered homepage is excluded because the browser already loaded it.
    assert summary["internal_urls_checked"] == 5
    assert summary["sitemap_url_count"] == 1
    issues = {issue["id"]: issue for issue in facts["issues"]}
    assert issues["client_error_internal_urls"]["examples"] == [f"{base}/missing"]
    assert issues["server_error_internal_urls"]["examples"] == [f"{base}/error"]


def test_private_hosts_blocked_without_allowance() -> None:
    facts = collect_site_health_facts(
        url="https://example.com/",
        seo_facts={"status": "complete", "pages": []},
        crawled_pages={
            "final_url": "https://example.com/",
            "discovered_links": [{"url": "http://127.0.0.1:9/internal"}],
        },
        rendered_pages=None,
        settings=_settings(crawler_allow_private_hosts=False, site_health_sitemap_max_urls=0),
    )

    assert facts["status"] == "complete"
    assert facts["summary"]["client_error_internal_urls"] == 0
    assert facts["summary"]["unreachable_internal_urls"] == 0
    assert facts["checks"]["blocked_urls"] == 1


def test_link_sweep_blocks_redirects_to_private_hosts(monkeypatch) -> None:
    monkeypatch.setattr(
        site_health,
        "_resolve_host_ips",
        lambda hostname: [ipaddress.ip_address("93.184.216.34")],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://public.example/redirect"
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/internal"},
            request=request,
        )

    async def run() -> tuple[dict, dict]:
        summary: dict = {"unreachable_internal_urls": 0}
        examples: dict = defaultdict(list)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            counters = await site_health._sweep(
                client,
                internal_urls=["http://public.example/redirect"],
                external_urls=[],
                settings=_settings(crawler_allow_private_hosts=False),
                summary=summary,
                examples=examples,
                notes=[],
                host_allowed_cache={},
            )
        return counters, summary

    counters, summary = asyncio.run(run())

    assert counters["blocked"] == 1
    assert counters["internal_checked"] == 0
    assert summary["unreachable_internal_urls"] == 0


def test_sweep_counts_internal_redirect_chains() -> None:
    # /a -> /b -> /final (2 hops = a chain); /one -> /final (1 hop = a single redirect, not chain).
    chain = {
        "http://site.example/a": ("http://site.example/b", 301),
        "http://site.example/b": ("http://site.example/final", 301),
        "http://site.example/one": ("http://site.example/final", 301),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if path in chain:
            location, status = chain[path]
            return httpx.Response(status, headers={"Location": location}, request=request)
        return httpx.Response(200, request=request)

    async def run() -> dict:
        summary: dict = {}
        examples: dict = defaultdict(list)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await site_health._sweep(
                client,
                internal_urls=["http://site.example/a", "http://site.example/one"],
                external_urls=[],
                settings=_settings(),
                summary=summary,
                examples=examples,
                notes=[],
                host_allowed_cache={},
            )
        return summary

    summary = asyncio.run(run())
    # Both redirected, so both count as "redirecting"; only the 2-hop /a is a chain.
    assert summary["redirecting_internal_urls"] == 2
    assert summary["redirect_chain_internal_urls"] == 1
    # Neither is a broken link — they all resolve to a final 200.
    assert summary.get("client_error_internal_urls", 0) == 0


def test_sweep_does_not_report_bot_blocked_external_links() -> None:
    """Outbound 401/403/429/451 are bot-blocking, not broken links.

    Regression: Spotify / Cloudflare-walled URLs returned 4xx to the audit's
    server-side bot and were wrongly listed under "Outbound links returning
    'not found' or blocked errors (4xx)" in a client-facing report. Only the
    unambiguous "page is gone" codes (404/410) may surface for outbound links;
    everything else is inconclusive and recorded for QA only.
    """
    path_status = {"/403": 403, "/429": 429, "/404": 404, "/ok": 200}
    external_urls = [
        "https://blocked.example/403",
        "https://ratelimited.example/429",
        "https://gone.example/404",
        "https://live.example/ok",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(path_status[request.url.path], request=request)

    async def run() -> tuple[dict, dict, dict]:
        summary: dict = {}
        examples: dict = defaultdict(list)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            counters = await site_health._sweep(
                client,
                internal_urls=[],
                external_urls=external_urls,
                settings=_settings(),
                summary=summary,
                examples=examples,
                notes=[],
                host_allowed_cache={},
            )
        return counters, summary, examples

    counters, summary, examples = asyncio.run(run())

    assert counters["external_checked"] == 4
    # Only the genuine "page gone" 404 counts as a broken outbound link.
    assert summary.get("client_error_external_urls", 0) == 1
    assert examples["client_error_external_urls"] == ["https://gone.example/404"]
    # 403 + 429 are bot-blocking: tallied for QA, never surfaced as broken.
    assert counters["inconclusive_external"] == 2
    surfaced = set(examples["client_error_external_urls"])
    assert not any("blocked.example" in u or "ratelimited.example" in u for u in surfaced)


def test_sitemap_fetch_blocks_redirects_to_private_hosts(monkeypatch) -> None:
    monkeypatch.setattr(
        site_health,
        "_resolve_host_ips",
        lambda hostname: [ipaddress.ip_address("93.184.216.34")],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://public.example/sitemap.xml"
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/sitemap.xml"},
            request=request,
        )

    async def run() -> tuple[set[str], str]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            return await site_health._fetch_sitemap(
                client,
                "http://public.example/sitemap.xml",
                "http://public.example/",
                settings=_settings(crawler_allow_private_hosts=False),
                host_allowed_cache={},
                depth=0,
            )

    urls, status = asyncio.run(run())

    assert urls == set()
    assert status == "blocked_host"


def test_external_seo_uses_site_health_when_screaming_frog_disabled() -> None:
    facts = collect_external_seo_facts(
        url="https://example.com/",
        audit_id="audit-1",
        page_urls=["https://example.com/"],
        settings=_settings(
            screaming_frog_enabled=False,
            site_health_sitemap_max_urls=0,
            google_oauth_client_id="",
            google_oauth_client_secret=None,
        ),
        db=None,
        seo_facts=_seo_facts_with_page_problems(),
        crawled_pages={"final_url": "https://example.com/"},
        rendered_pages=None,
    )

    assert facts["sources"]["technical_crawl"] == "complete"
    assert facts["sources"]["technical_crawl_tool"] == "site_health_sweep"
    assert facts["technical_crawl"]["summary"]["duplicate_titles"] == 2
    assert facts["gsc"]["status"] == "skipped"


def test_partial_sweep_preferred_over_failed_screaming_frog(monkeypatch) -> None:
    # Prod runs with Screaming Frog ENABLED but the binary is not installed, so SF
    # fails; the sweep then returns partial:bot_blocked (a WAF throttled it). The
    # partial sweep still carries the links it DID check plus the honest bot-block
    # note, so it must win over the SF failure (which used to blank the whole section).
    from apps.worker.stages import external_seo

    def _failed_sf(url, audit_id, settings, deadline=None):
        return {"status": "failed", "error": "screaming frog binary not found", "summary": {}}

    def _partial_sweep(**kwargs):
        return {
            "status": "partial",
            "reason": "bot_blocked",
            "summary": {"duplicate_titles": 1},
            "issues": [],
            "notes": ["Link checks stopped early after repeated blocks."],
        }

    monkeypatch.setattr(external_seo, "collect_screaming_frog_facts", _failed_sf)
    monkeypatch.setattr(external_seo, "collect_site_health_facts", _partial_sweep)

    facts = external_seo._collect_technical_crawl(
        url="https://example.com/",
        audit_id="audit-1",
        settings=_settings(screaming_frog_enabled=True),
        seo_facts={"status": "complete", "pages": []},
        crawled_pages={"final_url": "https://example.com/"},
        rendered_pages=None,
    )

    assert facts["status"] == "partial"
    assert facts["reason"] == "bot_blocked"
    # The SF attempt is recorded as context, not shown as the section's whole content.
    assert facts["screaming_frog_attempt"]["status"] == "failed"
    assert any("stopped early" in note for note in facts["notes"])


def test_failed_screaming_frog_still_shown_when_sweep_also_fails(monkeypatch) -> None:
    # When BOTH tools produce nothing usable, the operator-enabled SF failure is what
    # the section reports (unchanged behavior — the fix only rescues a partial sweep).
    from apps.worker.stages import external_seo

    def _failed_sf(url, audit_id, settings, deadline=None):
        return {"status": "failed", "error": "screaming frog binary not found", "summary": {}}

    def _failed_sweep(**kwargs):
        return {"status": "failed", "reason": "unknown_error", "summary": {}, "issues": []}

    monkeypatch.setattr(external_seo, "collect_screaming_frog_facts", _failed_sf)
    monkeypatch.setattr(external_seo, "collect_site_health_facts", _failed_sweep)

    facts = external_seo._collect_technical_crawl(
        url="https://example.com/",
        audit_id="audit-1",
        settings=_settings(screaming_frog_enabled=True),
        seo_facts={"status": "complete", "pages": []},
        crawled_pages={"final_url": "https://example.com/"},
        rendered_pages=None,
    )

    assert facts["status"] == "failed"
    assert facts["error"] == "screaming frog binary not found"


def test_breaker_trips_and_reports_bot_blocked(monkeypatch) -> None:
    # Consecutive connection failures (a WAF dropping the checker, or a dead host) must
    # stop the sweep and mark the source partial:bot_blocked instead of emitting mass
    # false "dead link" findings — the non-complete status keeps it out of scoring.
    async def _fake_recheck(url, settings):
        return True

    monkeypatch.setattr(site_health, "_browser_ua_recheck", _fake_recheck)
    dead = [{"url": f"http://127.0.0.1:9/page-{i}"} for i in range(10)]
    facts = collect_site_health_facts(
        url="http://127.0.0.1:9/",
        seo_facts={"status": "complete", "pages": []},
        crawled_pages={"final_url": "http://127.0.0.1:9/", "discovered_links": dead},
        rendered_pages=None,
        settings=_settings(
            site_health_breaker_threshold=3,
            site_health_per_host_concurrency=1,
            site_health_sitemap_max_urls=0,
        ),
    )

    assert facts["status"] == "partial"
    assert facts["reason"] == "bot_blocked"
    assert facts["checks"]["browser_recheck_ok"] is True
    # The breaker stopped the run: at most threshold failures counted, the rest skipped.
    assert facts["summary"]["unreachable_internal_urls"] <= 4
    assert facts["checks"]["skipped_breaker"] >= 1
    assert facts["summary"]["unreachable_error_classes"].get("connection_failed", 0) >= 3
    assert any("stopped early" in note for note in facts["notes"])
    assert any("regular browser profile" in note for note in facts["notes"])


def test_breaker_resets_on_success_between_failures(monkeypatch) -> None:
    # "Consecutive" means consecutive: failures interleaved with working URLs must never
    # trip the breaker (4 total failures here >= threshold 3, but the max run is 2).
    async def _fake_recheck(url, settings):
        return True

    monkeypatch.setattr(site_health, "_browser_ua_recheck", _fake_recheck)
    with _serve() as base:
        # The sweep sorts its check list, so interleave by SORTED path order:
        # a-drop, b-ok, c-drop, d-ok, e-drop, f-drop -> max failure run of 2.
        links = [
            {"url": f"{base}/a-drop"},
            {"url": f"{base}/b-ok"},
            {"url": f"{base}/c-drop"},
            {"url": f"{base}/d-ok"},
            {"url": f"{base}/e-drop"},
            {"url": f"{base}/f-drop"},
        ]
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={"final_url": f"{base}/", "discovered_links": links},
            rendered_pages=None,
            settings=_settings(
                site_health_breaker_threshold=3,
                site_health_per_host_concurrency=1,
                site_health_sitemap_max_urls=0,
            ),
        )

    assert facts["status"] == "complete"
    assert "reason" not in facts
    assert facts["checks"]["skipped_breaker"] == 0
    assert facts["summary"]["unreachable_internal_urls"] == 4


def test_browser_recheck_never_follows_redirects(monkeypatch) -> None:
    # The recheck's sample URL was SSRF-vetted at sweep time, but a redirect target is
    # attacker-controlled (open redirect -> cloud metadata). The client must not follow
    # redirects — a 3xx already proves the server answers a browser profile.
    captured: dict = {}

    class _FakeResponse:
        status_code = 302

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(site_health.httpx, "AsyncClient", _FakeClient)
    ok = asyncio.run(site_health._browser_ua_recheck("https://example.com/x", _settings()))
    assert ok is True  # 302 counts as "answered"
    assert captured["follow_redirects"] is False


def test_browser_recheck_reraises_soft_time_limit(monkeypatch) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            raise SoftTimeLimitExceeded()

    monkeypatch.setattr(site_health.httpx, "AsyncClient", _FakeClient)
    with pytest.raises(SoftTimeLimitExceeded):
        asyncio.run(site_health._browser_ua_recheck("https://example.com/x", _settings()))


def test_healthy_sweep_stays_complete_and_reports_rate() -> None:
    with _serve() as base:
        crawled_pages = {
            "final_url": f"{base}/",
            "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
            "discovered_links": [{"url": f"{base}/ok"}],
        }
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages=crawled_pages,
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0),
        )

    assert facts["status"] == "complete"
    assert "reason" not in facts
    assert any("ran politely" in note for note in facts["notes"])


def test_rate_limited_url_is_retried_after_retry_after(monkeypatch) -> None:
    # A 429 must be honored (Retry-After) and retried — never insta-hammered with a GET.
    class _Handler429(_Handler):
        attempts: dict[str, int] = {}

        def _respond(self, include_body: bool) -> None:
            if self.path == "/limited":
                count = self.attempts.get(self.path, 0) + 1
                self.attempts[self.path] = count
                if count == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"ok now"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if include_body:
                    self.wfile.write(body)
                return
            super()._respond(include_body)

    handler = functools.partial(_Handler429)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={
                "final_url": f"{base}/",
                "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
                "discovered_links": [{"url": f"{base}/limited"}],
            },
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0),
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert facts["status"] == "complete"
    # The retried request succeeded, so the URL is NOT a client error or unreachable.
    assert facts["summary"]["client_error_internal_urls"] == 0
    assert facts["summary"]["unreachable_internal_urls"] == 0
    assert facts["summary"]["internal_urls_checked"] == 1


class _HeadOnly429(_Handler):
    # A CDN profile that rate-limits HEAD specifically while serving GET fine.
    routes = {**_Handler.routes, "/head-limited": (200, b"fine over GET")}

    def _respond(self, include_body: bool) -> None:
        if self.path == "/head-limited" and self.command == "HEAD":
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super()._respond(include_body)


class _Always429(_Handler):
    # A host throttling EVERY method: the checker must report "rate limited", not "broken".
    def _respond(self, include_body: bool) -> None:
        if self.path == "/throttled":
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super()._respond(include_body)


def test_persistent_head_429_falls_back_to_get() -> None:
    # A CDN/WAF that rate-limits HEAD specifically while serving GET fine: after the polite
    # Retry-After retries are exhausted, one GET must recover the URL — otherwise dozens of
    # healthy pages land in client_error_internal_urls and get scored as broken.
    with _serve(_HeadOnly429) as base:
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={
                "final_url": f"{base}/",
                "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
                "discovered_links": [{"url": f"{base}/head-limited"}],
            },
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0),
        )

    assert facts["status"] == "complete"
    assert facts["summary"]["client_error_internal_urls"] == 0
    assert facts["summary"]["unreachable_internal_urls"] == 0
    assert facts["summary"]["internal_urls_checked"] == 1


def test_final_429_is_inconclusive_and_widespread_throttling_goes_partial() -> None:
    # A URL still 429ing after every polite retry must NOT be scored as a broken client
    # error, and when throttling is widespread the whole source downgrades to `partial`
    # (reason rate_limited) so the trust gate strips the summary instead of scoring it.
    with _serve(_Always429) as base:
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={
                "final_url": f"{base}/",
                "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
                "discovered_links": [{"url": f"{base}/throttled"}],
            },
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0, site_health_breaker_threshold=1),
        )

    assert facts["summary"]["client_error_internal_urls"] == 0
    assert facts["checks"]["rate_limited_internal_urls"] == 1
    assert facts["status"] == "partial"
    assert facts["reason"] == "rate_limited"
    # The client-facing note says throttled URLs are "reported as unchecked" — the rendered
    # "Links checked" total must agree (the throttled URL is NOT counted as checked).
    assert facts["summary"]["internal_urls_checked"] == 0
    assert facts["summary"]["urls_checked"] == 0


def test_scope_counts_one_page_per_redirected_render() -> None:
    # The client-facing "Pages discovered" must count a redirected page ONCE (its final URL),
    # not once per pre/post-redirect form — a one-page http->https+www site is 1 page, not 2.
    facts = collect_site_health_facts(
        url="http://example.com/",
        seo_facts={"status": "complete", "pages": []},
        crawled_pages={
            "final_url": "https://www.example.com/",
            "pages": [{"url": "http://example.com/", "final_url": "https://www.example.com/"}],
            "discovered_links": [],
        },
        rendered_pages=None,
        settings=_settings(site_health_sitemap_max_urls=0),
    )
    assert facts["summary"]["discovered_internal_urls"] == 1


def test_first_request_per_host_skips_politeness_delay() -> None:
    # Politeness spacing is per HOST: the first request to a host (every outbound link's only
    # request) goes straight out. With a 10s delay and a single sweep URL, the old
    # unconditional per-request sleep would blow this assertion.
    with _serve() as base:
        started = time.monotonic()
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={
                "final_url": f"{base}/",
                "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
                "discovered_links": [{"url": f"{base}/ok"}],
            },
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0, site_health_request_delay_ms=10_000),
        )
        elapsed = time.monotonic() - started
    assert facts["summary"]["internal_urls_checked"] == 1
    assert elapsed < 5


def test_same_host_requests_keep_politeness_spacing() -> None:
    # Repeat requests to the SAME host still pace: 3 same-host URLs with a 600ms delay must
    # sleep for at least one jittered gap (>= 0.75 * 600ms), so the sweep cannot finish
    # near-instantly the way distinct-host checks do.
    with _serve() as base:
        started = time.monotonic()
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={
                "final_url": f"{base}/",
                "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
                "discovered_links": [
                    {"url": f"{base}/ok"},
                    {"url": f"{base}/missing"},
                    {"url": f"{base}/noindexed"},
                ],
            },
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0, site_health_request_delay_ms=600),
        )
        elapsed = time.monotonic() - started
    assert facts["summary"]["internal_urls_checked"] == 3
    assert elapsed >= 0.4


def test_rate_limited_url_contributes_no_scored_redirect_facts() -> None:
    # A URL that redirects and THEN persistently 429s is "reported as unchecked" — it must not
    # feed the SCORED redirect facts either, or issue counts could exceed the rendered
    # "Links checked" total (a self-contradicting report).
    hops = {
        "http://site.example/a": "http://site.example/b",
        "http://site.example/b": "http://site.example/throttled",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if path in hops:
            return httpx.Response(301, headers={"Location": hops[path]}, request=request)
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

    async def run() -> tuple[dict, dict]:
        summary: dict = {}
        examples: dict = defaultdict(list)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            counters = await site_health._sweep(
                client,
                internal_urls=["http://site.example/a"],
                external_urls=[],
                settings=_settings(),
                summary=summary,
                examples=examples,
                notes=[],
                host_allowed_cache={},
            )
        return summary, counters

    summary, counters = asyncio.run(run())
    assert counters["rate_limited_internal"] == 1
    assert counters["internal_checked"] == 0
    assert summary.get("redirecting_internal_urls", 0) == 0
    assert summary.get("redirect_chain_internal_urls", 0) == 0


def test_small_site_fully_throttled_goes_partial_below_absolute_threshold() -> None:
    # A site with fewer internal URLs than the absolute breaker threshold that 429s every
    # check must still downgrade to `partial` (majority rule): zero-coverage link results
    # must never be scored as clean under a "complete" status.
    with _serve(_Always429) as base:
        facts = collect_site_health_facts(
            url=f"{base}/",
            seo_facts={"status": "complete", "pages": []},
            crawled_pages={
                "final_url": f"{base}/",
                "pages": [{"url": f"{base}/", "final_url": f"{base}/"}],
                "discovered_links": [{"url": f"{base}/throttled"}],
            },
            rendered_pages=None,
            settings=_settings(site_health_sitemap_max_urls=0),  # default threshold (5) stays
        )

    assert facts["checks"]["rate_limited_internal_urls"] == 1
    assert facts["summary"]["internal_urls_checked"] == 0
    assert facts["status"] == "partial"
    assert facts["reason"] == "rate_limited"


def test_zero_coverage_from_refusals_is_never_scored_as_clean() -> None:
    # Every internal URL refused the checker (transport failures below the breaker threshold):
    # zero usable coverage must NOT be reported as a "complete" clean sweep — the trust gate
    # would score the empty summary as a full pass.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def run() -> dict:
        summary: dict = {}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            return await site_health._sweep(
                client,
                internal_urls=["http://site.example/a", "http://site.example/b"],
                external_urls=[],
                settings=_settings(),
                summary=summary,
                examples=defaultdict(list),
                notes=[],
                host_allowed_cache={},
            )

    counters = asyncio.run(run())
    assert counters["internal_conclusive"] == 0


def test_one_throttled_url_beside_a_healthy_one_stays_complete() -> None:
    # Throttling must DOMINATE what answered before the whole source is downgraded: a 50/50
    # split on a two-URL site is not "widespread", and downgrading there would strip a real
    # finding from the URL that did answer.
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/throttled"):
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, request=request)

    async def run() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            return await site_health._sweep(
                client,
                internal_urls=["http://site.example/ok", "http://site.example/throttled"],
                external_urls=[],
                settings=_settings(),
                summary={},
                examples=defaultdict(list),
                notes=[],
                host_allowed_cache={},
            )

    counters = asyncio.run(run())
    assert counters["rate_limited_internal"] == 1
    assert counters["internal_conclusive"] == 1
