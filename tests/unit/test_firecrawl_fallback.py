"""The Firecrawl fallback (apps/worker/stages/firecrawl_fallback.py): when the audited site's
security blocks our browser on the homepage, the pages are fetched through Firecrawl instead.

Nothing here reaches the network: Firecrawl and its screenshot storage answer through an httpx
MockTransport (any other address fails the test), Redis is an in-memory double, DNS is stubbed,
and the browser crawl runs through the crawler's own seams with its homepage render faked."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.shared.audit_states import AuditStatus
from apps.shared.config import Settings
from apps.shared.models import AuditJob, Base
from apps.worker import tasks
from apps.worker.stages import crawler, firecrawl_fallback
from apps.worker.stages.crawler import (
    BOT_CHECK_BLOCKED_MESSAGE,
    BOT_CHECK_PAGE_REASON,
    CrawledPage,
    CrawlerError,
    RobotsPolicy,
    SiteBlockedError,
    _bot_check_error,
    _http_error,
    http_error_message,
    site_security_block,
)
from apps.worker.stages.extractor_uxui import extract_uxui_facts_for_page
from apps.worker.stages.firecrawl_fallback import (
    PAGE_FETCH_FAILED_REASON,
    count_fallback_audit,
    scrape_request_body,
)
from apps.worker.stages.report_payload import FETCHED_THROUGH_SERVICE_NOTE, compose_report_payload

KEY = "fc-unit-test-7d3f9a1c5e8b2d4f-NEVER-LOGGED"
HOST = "www.acme-builders.example"
SITE = f"https://{HOST}"
API = "https://api.firecrawl.dev/v1/scrape"
SHOTS = "storage.firecrawl.example"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

# SiteGround's two answers as captured from the server on 30 September 2026 (our address replaced
# by a documentation one, the challenge's proof-of-work script left out). First the 202 whose
# meta refresh starts the check...
SG_REFRESH_PAGE = (
    '<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh" '
    'content="0;/.well-known/sgcaptcha/?r=%2F&y=ipc:203.0.113.9:1790749657.771"></meta>'
    "</head></html>"
)
# ...then the "Robot Challenge Screen" it leads to.
SG_CHALLENGE_PAGE = (
    '<!doctype html><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en"><head>'
    '<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1">'
    '<meta name="ROBOTS" content="NOINDEX, NOFOLLOW"><noscript><meta http-equiv="refresh" '
    'content="0;/.well-known/captcha/?r=%2F"></noscript><title>Robot Challenge Screen</title>'
    '<link rel="icon" href="data:;"><script>const sgchallenge="21:1790749669:5dade533:0b95100a3a'
    'aa1c1ae1f29e08a67e56e41ad55e741126e017f3843b85b6ffc7a9:";\nconst sgsubmit_url="/.well-known'
    '/sgcaptcha/?r=%2F";const sgfallback_url="/.well-known/captcha/?y=err&r=%2F";</script>'
    '</head><body style="text-align: center; margin: 0; padding: 0; height: 100%; color: #363636;'
    '"><section><div class="head-image"><img style="width: 240px;" src="https://d1rozh26tys225.'
    'cloudfront.net/robot-suspicion.svg" alt="Robot"></div><div id="powCaptcha"><h1>'
    f"{HOST}</h1><p>Checking the site connection security</p></div></section><footer><p>This "
    "page requires cookies to be enabled in your browser settings. Please check this setting and "
    "enable cookies (if disabled)</p></footer></body></html>"
)


def _page_html(title: str, *links: str, extra: str = "") -> str:
    anchors = "".join(f'<a href="{href}">{href.strip("/") or "Home"}</a>' for href in links)
    return (
        f'<!DOCTYPE html><html lang="en-US"><head><title>{title}</title>'
        '<meta name="description" content="Custom homes built around the way you live.">'
        f'<link rel="canonical" href="{SITE}/"></head><body><header><nav>{anchors}</nav></header>'
        f"<main><h1>{title}</h1><p>We design and build custom homes across the Twin Cities.</p>"
        f'<button class="btn cta" type="button">Schedule a call</button>{extra}</main>'
        "<script>window.dataLayer = [];</script><footer>Call 763-555-0100</footer></body></html>"
    )


def _scraped(url: str, html: str, *, status: int = 200, final_url: str | None = None) -> dict:
    """A /v1/scrape success shaped like the real answer captured from Firecrawl on 1 October."""
    final_url = final_url or url
    digest = hashlib.sha256(final_url.encode()).hexdigest()[:12]
    return {
        "success": True,
        "data": {
            "rawHtml": html,
            "screenshot": f"https://{SHOTS}/media/screenshot-{digest}.png",
            "metadata": {
                "title": "from metadata",
                "language": "en-US",
                "sourceURL": url,
                "url": final_url,
                "statusCode": status,
                "contentType": "text/html; charset=UTF-8",
                "proxyUsed": "basic",
                "cacheState": "miss",
                "creditsUsed": 1,
            },
        },
    }


class FakeFirecrawl:
    """Firecrawl's API and its screenshot storage behind one MockTransport. ``pages`` maps a
    requested address to the JSON Firecrawl answers (or an httpx.Response); an address it does not
    list answers like an ordinary page (the homepage links /about-us/). Any other host fails the
    test: nothing may reach the audited site or the network."""

    def __init__(self, pages: dict | None = None, shot: bytes | httpx.Response = PNG) -> None:
        self.pages = pages or {}
        self.shot = shot
        self.requests: list[httpx.Request] = []

    @property
    def scraped(self) -> list[str]:
        return [
            json.loads(request.content)["url"]
            for request in self.requests
            if request.url.host == "api.firecrawl.dev"
        ]

    @property
    def bodies(self) -> list[dict]:
        return [
            json.loads(request.content)
            for request in self.requests
            if request.url.host == "api.firecrawl.dev"
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == API and request.method == "POST":
            url = json.loads(request.content)["url"]
            answer = self.pages.get(url)
            if isinstance(answer, httpx.Response):
                return answer
            if answer is None:
                path = urlparse(url).path or "/"
                links = ("/about-us/",) if path == "/" else ()
                answer = _scraped(url, _page_html(f"Acme {path}", *links))
            return httpx.Response(200, json=answer)
        if request.url.host == SHOTS:
            if isinstance(self.shot, httpx.Response):
                return self.shot
            return httpx.Response(200, content=self.shot, headers={"content-type": "image/png"})
        raise AssertionError(f"unexpected request to {request.url}")


class FakeRedis:
    """The two Redis calls the daily counter makes (a MULTI/EXEC pipeline of INCR + EXPIRE)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> FakeRedis._Pipeline:
        assert transaction is True
        return FakeRedis._Pipeline(self)

    def close(self) -> None:
        pass

    class _Pipeline:
        def __init__(self, store: FakeRedis) -> None:
            self._store = store
            self._ops: list[tuple] = []

        def __enter__(self) -> FakeRedis._Pipeline:
            return self

        def __exit__(self, *_exc) -> bool:
            return False

        def incr(self, key: str) -> None:
            self._ops.append(("incr", key))

        def expire(self, key: str, seconds: int) -> None:
            self._ops.append(("expire", key, seconds))

        def execute(self) -> list:
            results: list = []
            for op in self._ops:
                if op[0] == "incr":
                    self._store.counts[op[1]] = self._store.counts.get(op[1], 0) + 1
                    results.append(self._store.counts[op[1]])
                else:
                    self._store.ttls[op[1]] = op[2]
                    results.append(True)
            return results


class _Context:
    def set_default_timeout(self, _ms: int) -> None:
        pass

    def set_default_navigation_timeout(self, _ms: int) -> None:
        pass

    async def route(self, _pattern: str, _handler) -> None:
        pass

    async def close(self) -> None:
        pass


class _Browser:
    async def new_context(self, **_kwargs) -> _Context:
        return _Context()

    async def close(self) -> None:
        pass


class _PlaywrightCM:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Public DNS for every host, a fresh Firecrawl double and Redis double, a robots.txt that
    allows everything, and a browser whose homepage render is decided by ``world.homepage``
    (an exception to raise, or None for an ordinary page)."""
    service = FakeFirecrawl()
    store = FakeRedis()
    state = SimpleNamespace(
        firecrawl=service,
        redis=store,
        homepage=_bot_check_error("SiteGround CAPTCHA", own_site=True),
        rendered=[],
        robots=RobotsPolicy(status="unavailable", robots_url=f"{SITE}/robots.txt"),
        tmp_path=tmp_path,
    )
    real_client = httpx.AsyncClient

    def _client(**kwargs):
        return real_client(transport=httpx.MockTransport(service.handler), **kwargs)

    async def _fake_launch(_playwright, _settings):
        return _Browser()

    async def _fake_robots(_url, _settings):
        return state.robots

    async def _fake_render(_context, url, _settings, _audit_id, source_url=None, link_score=None):
        state.rendered.append(url)
        if source_url is None and state.homepage is not None:
            raise state.homepage
        return CrawledPage(
            url=url,
            final_url=url,
            status_code=200,
            title="Acme Builders",
            html=_page_html("Acme Builders", "/about-us/"),
            text="Acme Builders",
            fetched_at="2026-10-02T00:00:00+00:00",
        )

    monkeypatch.setattr(firecrawl_fallback.httpx, "AsyncClient", _client)
    monkeypatch.setattr(firecrawl_fallback, "_redis_client", lambda _settings: store)
    monkeypatch.setattr(
        crawler, "_resolve_host_ips", lambda _host: [ipaddress.ip_address("93.184.216.34")]
    )
    monkeypatch.setattr(crawler, "_launch_chromium", _fake_launch)
    monkeypatch.setattr(crawler, "load_robots_policy", _fake_robots)
    monkeypatch.setattr(crawler, "_render_page", _fake_render)
    monkeypatch.setattr(crawler.playwright_api, "async_playwright", lambda: _PlaywrightCM())
    return state


def _settings(world, **overrides) -> Settings:
    values = {
        "firecrawl_api_key": KEY,
        "crawler_allow_private_hosts": False,
        "crawler_concurrency": 1,
        "crawler_challenge_wait_seconds": 0,
        "crawler_screenshots_enabled": True,
        "local_screenshot_storage_dir": world.tmp_path / "screenshots",
    }
    values.update(overrides)
    return Settings(**values)


def _crawl(world, url: str = f"{SITE}/", **overrides):
    return asyncio.run(crawler.crawl_site(url, _settings(world, **overrides), "job-1"))


def _blocked(world, **overrides) -> SiteBlockedError:
    with pytest.raises(SiteBlockedError) as excinfo:
        _crawl(world, **overrides)
    return excinfo.value


# --- The fallback runs, and builds what the browser crawl builds -------------------------------


def test_a_blocked_homepage_is_audited_from_pages_fetched_through_firecrawl(world) -> None:
    home_html = _page_html(
        "Custom Home Builder | Acme Builders",
        "/about-us/",
        "/gallery/",
        "/contact-us/",
        extra='<iframe src="https://api.leadconnectorhq.com/widget/form/abc"></iframe>',
    )
    world.firecrawl.pages[f"{SITE}/"] = _scraped(f"{SITE}/", home_html)

    result = _crawl(world)

    # The browser opened only the homepage; Firecrawl fetched it, then the internal pages in the
    # browser crawl's own ranking.
    assert world.rendered == [f"{SITE}/"]
    ranked = [link.url for link in crawler.discover_internal_links(home_html, f"{SITE}/")]
    assert sorted(ranked) == [f"{SITE}/about-us", f"{SITE}/contact-us", f"{SITE}/gallery"]
    assert world.firecrawl.scraped == [f"{SITE}/", *ranked]
    assert result.status == "complete"
    assert result.fetched_via == "firecrawl"
    assert result.browser_blocked_by == "SiteGround CAPTCHA"
    assert [page.final_url for page in result.pages] == world.firecrawl.scraped
    home = result.pages[0]
    assert home.html == home_html  # the real markup, <head> included, for every analyser
    assert home.title == "Custom Home Builder | Acme Builders"
    assert home.status_code == 200
    assert home.source_url is None and home.link_score is None
    assert "We design and build custom homes" in home.text
    assert "dataLayer" not in home.text  # scripts are not page text
    # What needs a live page gets the analysers' own "not measured" values: the form embed is
    # still credited from its provider signature in the real HTML.
    assert home.frame_form_count == 0 and home.axe_results is None
    assert extract_uxui_facts_for_page(home)["forms"]["form_detected"] == "provider_embed"
    assert home.passed_interstitial is None
    assert all(page.fetched_via == "firecrawl" for page in result.pages)
    assert {page.source_url for page in result.pages[1:]} == {f"{SITE}/"}
    assert all(page.link_score is not None for page in result.pages[1:])

    stored = result.to_dict()
    assert stored["fetched_via"] == "firecrawl"
    assert stored["browser_blocked_by"] == "SiteGround CAPTCHA"
    assert stored["summary"]["successful_pages"] == 4
    assert {page["fetched_via"] for page in stored["pages"]} == {"firecrawl"}


def test_screenshots_are_downloaded_into_the_crawlers_own_files(world) -> None:
    result = _crawl(world)

    for page in result.pages:
        expected = crawler._screenshot_path(_settings(world), "job-1", page.final_url)
        assert page.screenshot_path == str(expected)
        assert page.screenshot_error is None
        assert expected.read_bytes() == PNG
    downloads = [r for r in world.firecrawl.requests if r.url.host == SHOTS]
    assert len(downloads) == len(result.pages)
    # The key goes to Firecrawl's API only, never to the screenshot storage.
    assert all("authorization" not in request.headers for request in downloads)
    assert all(
        request.headers["authorization"] == f"Bearer {KEY}"
        for request in world.firecrawl.requests
        if request.url.host == "api.firecrawl.dev"
    )


def test_the_request_is_standard_firecrawl_only(world) -> None:
    _crawl(world)
    assert world.firecrawl.bodies[0] == {
        "url": f"{SITE}/",
        "formats": ["rawHtml", "screenshot@fullPage"],
        "proxy": "basic",
        "blockAds": False,
        "skipTlsVerification": True,
        "timeout": 45000,
    }
    body = scrape_request_body(f"{SITE}/", _settings(world, crawler_screenshots_enabled=False))
    assert body["formats"] == ["rawHtml"]
    assert "stealth" not in json.dumps(world.firecrawl.bodies)
    assert "actions" not in world.firecrawl.bodies[0]


def test_screenshots_off_means_none_requested_or_stored(world) -> None:
    result = _crawl(world, crawler_screenshots_enabled=False)
    assert all(page.screenshot_path is None for page in result.pages)
    assert all(page.screenshot_error is None for page in result.pages)
    assert not [r for r in world.firecrawl.requests if r.url.host == SHOTS]


@pytest.mark.parametrize(
    ("max_pages", "firecrawl_max_pages", "expected"),
    [(10, 3, 3), (2, 10, 2), (10, 1, 1)],
)
def test_the_page_cap_is_the_smaller_of_both_caps(
    world, max_pages, firecrawl_max_pages, expected
) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(
        f"{SITE}/", _page_html("Acme", "/a/", "/b/", "/c/", "/d/", "/e/")
    )
    result = _crawl(
        world, crawler_max_pages=max_pages, crawler_firecrawl_max_pages=firecrawl_max_pages
    )
    assert len(world.firecrawl.scraped) == expected
    assert len(result.pages) == expected
    assert result.max_pages == expected


def test_child_pages_are_chosen_like_the_browser_crawl_same_site_only(world) -> None:
    links = (
        "/about-us/",
        "https://acme-builders.example/floor-plans/",  # the bare domain is the same site
        "https://blog.acme-builders.example/news",  # another subdomain is not
        "https://instagram.com/acme_builders/",
        "https://www.builderleadconverter.com/",
        "tel:+17635550100",
        "#reviews",
        "/private/area",
    )
    world.firecrawl.pages[f"{SITE}/"] = _scraped(f"{SITE}/", _page_html("Acme", *links))
    world.robots = RobotsPolicy(status="loaded", robots_url=f"{SITE}/robots.txt")
    parser = crawler.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private"])
    world.robots = replace(world.robots, parser=parser)

    result = _crawl(world)

    assert world.firecrawl.scraped == [
        f"{SITE}/",
        f"{SITE}/about-us",
        "https://acme-builders.example/floor-plans",
    ]
    assert all(crawler.is_same_site(f"{SITE}/", url) for url in world.firecrawl.scraped)
    assert [(page["url"], page["reason"]) for page in result.skipped_pages] == [
        (f"{SITE}/private/area", "disallowed_by_robots_txt")
    ]
    assert [link.url for link in result.discovered_links] == [
        link.url for link in crawler.discover_internal_links(result.pages[0].html, f"{SITE}/")
    ]


def test_a_403_refusal_of_the_homepage_also_falls_back(world) -> None:
    world.homepage = _http_error(403)
    result = _crawl(world)
    assert result.browser_blocked_by == "HTTP 403"
    assert len(result.pages) == 2


def test_the_whole_audit_completes_and_reports_how_pages_were_fetched(
    world, monkeypatch, tmp_path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(tasks, "SessionLocal", session)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: _settings(
            world,
            google_psi_api_key=None,
            site_health_enabled=False,
            screaming_frog_enabled=False,
            google_oauth_client_id="",
        ),
    )
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        tasks,
        "render_audit_pdf",
        lambda job, result, settings: tasks.PdfRenderResult(
            pdf_path=str(pdf_path),
            report_metadata={"status": "complete", "renderer": "weasyprint"},
            page_count=1,
            size_bytes=pdf_path.stat().st_size,
        ),
    )
    psi_urls: list[list[str]] = []

    def _psi(urls, settings):
        psi_urls.append(list(urls))
        return {"status": "skipped", "reason": "unit_test", "pages": [], "strategies": {}}

    with session() as db:
        job = AuditJob(url=f"{SITE}/", status=AuditStatus.QUEUED.value, progress_pct=0)
        db.add(job)
        db.commit()
        job_id = str(job.id)

    tasks.run_collection_audit(job_id, psi_collector=_psi)

    with session() as db:
        job = db.get(AuditJob, job_id)
        assert job.status == AuditStatus.COMPLETE.value
        assert job.error_message is None
        result = job.result
        assert result.crawled_pages["fetched_via"] == "firecrawl"
        assert result.seo_facts["pages_analyzed"] == 2
        assert result.uxui_facts["summary"]["total_ctas"] >= 1
        assert result.seo_score > 0
        payload = compose_report_payload(job, result, settings=_settings(world))
        assert payload.crawl_summary.note == FETCHED_THROUGH_SERVICE_NOTE
        assert "irecrawl" not in payload.model_dump_json()  # no vendor name for visitors
    # PageSpeed (fetched by Google) gets the real pages' addresses, never a check's.
    assert psi_urls == [[f"{SITE}/", f"{SITE}/about-us"]]


# --- Fallback off, or not for this failure: exactly the old behaviour ---------------------------


def test_without_a_key_the_plain_blocked_error_stands_and_firecrawl_is_never_called(world) -> None:
    original = world.homepage
    error = _blocked(world, firecrawl_api_key=None)
    assert error is original
    assert str(error) == BOT_CHECK_BLOCKED_MESSAGE
    assert world.firecrawl.requests == []
    assert world.redis.counts == {}


def test_an_empty_key_or_a_zero_daily_limit_is_off_too(world) -> None:
    assert _blocked(world, firecrawl_api_key="  ") is world.homepage
    assert _blocked(world, crawler_firecrawl_daily_limit=0) is world.homepage
    assert world.firecrawl.requests == []
    assert world.redis.counts == {}


def test_an_unblocked_site_never_calls_firecrawl(world) -> None:
    world.homepage = None
    result = _crawl(world)
    assert world.firecrawl.requests == []
    assert world.redis.counts == {}
    assert result.fetched_via is None
    stored = result.to_dict()
    assert "fetched_via" not in stored and "browser_blocked_by" not in stored
    assert all("fetched_via" not in page for page in stored["pages"])


@pytest.mark.parametrize("status", [404, 410, 429, 500, 503])
def test_other_http_errors_on_the_homepage_never_fall_back(world, status) -> None:
    world.homepage = _http_error(status)
    error = _blocked(world)
    assert str(error) == http_error_message(status)
    assert world.firecrawl.requests == []


def test_another_websites_check_or_a_timeout_never_falls_back(world) -> None:
    world.homepage = _bot_check_error("Cloudflare challenge", own_site=False)
    assert _blocked(world) is world.homepage
    world.homepage = CrawlerError(f"Timed out rendering {SITE}/")
    with pytest.raises(CrawlerError, match="Timed out"):
        _crawl(world)
    assert world.firecrawl.requests == []


def test_site_security_block_names_only_the_sites_own_wall() -> None:
    assert site_security_block(_bot_check_error("SiteGround CAPTCHA", own_site=True)) == (
        "SiteGround CAPTCHA"
    )
    assert site_security_block(_http_error(401)) == "HTTP 401"
    assert site_security_block(_http_error(403)) == "HTTP 403"
    assert site_security_block(_bot_check_error("Cloudflare challenge", own_site=False)) is None
    for status in (404, 410, 429, 500):
        assert site_security_block(_http_error(status)) is None
    assert site_security_block(SiteBlockedError(BOT_CHECK_BLOCKED_MESSAGE)) is None


# --- Caps -------------------------------------------------------------------------------------


def test_the_daily_limit_counts_fallback_audits_and_then_keeps_the_plain_error(world) -> None:
    for _ in range(2):
        _crawl(world, crawler_firecrawl_daily_limit=2)
    calls = len(world.firecrawl.requests)

    error = _blocked(world, crawler_firecrawl_daily_limit=2)

    assert error is world.homepage
    assert str(error) == BOT_CHECK_BLOCKED_MESSAGE
    assert len(world.firecrawl.requests) == calls  # nothing sent past the limit
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert world.redis.counts == {f"firecrawl_fallback:{today}": 3}


def test_the_daily_counter_is_one_dated_key_with_a_two_day_expiry(world) -> None:
    settings = _settings(world)
    day = datetime(2026, 10, 2, 23, 59, tzinfo=UTC)
    assert count_fallback_audit(settings, now=day) == 1
    assert count_fallback_audit(settings, now=day) == 2
    assert count_fallback_audit(settings, now=datetime(2026, 10, 3, 0, 1, tzinfo=UTC)) == 1
    assert world.redis.counts == {
        "firecrawl_fallback:2026-10-02": 2,
        "firecrawl_fallback:2026-10-03": 1,
    }
    assert set(world.redis.ttls.values()) == {2 * 24 * 60 * 60}


def test_no_redis_means_no_fallback(world, monkeypatch) -> None:
    def _down(_settings):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(firecrawl_fallback, "_redis_client", _down)
    assert _blocked(world) is world.homepage
    assert world.firecrawl.requests == []


# --- Firecrawl meets the wall too, or fails: the plain message ---------------------------------


@pytest.mark.parametrize(
    ("answer", "final_url"),
    [
        # SiteGround's 202: nothing but a meta refresh into its check.
        ({"status": 202, "html": SG_REFRESH_PAGE}, None),
        # The Robot Challenge Screen, still at the homepage's address.
        ({"status": 200, "html": SG_CHALLENGE_PAGE}, None),
        # Its human CAPTCHA fallback.
        ({"status": 200, "html": SG_CHALLENGE_PAGE}, f"{SITE}/.well-known/captcha/?y=err&r=%2F"),
        ({"status": 403, "html": "<html><head><title>403 Forbidden</title></head></html>"}, None),
        ({"status": 401, "html": "<html><body>Unauthorized</body></html>"}, None),
        (
            {"status": 403, "html": "<html><head><title>Just a moment...</title></head></html>"},
            None,
        ),
    ],
)
def test_firecrawl_shown_the_same_wall_fails_with_the_plain_message(
    world, answer, final_url
) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(
        f"{SITE}/", answer["html"], status=answer["status"], final_url=final_url
    )
    error = _blocked(world)
    assert error is world.homepage
    assert str(error) == BOT_CHECK_BLOCKED_MESSAGE
    assert world.firecrawl.scraped == [f"{SITE}/"]  # one attempt, no retry
    assert not [r for r in world.firecrawl.requests if r.url.host == SHOTS]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(402, json={"success": False, "error": "Insufficient credits"}),
        httpx.Response(429, json={"success": False, "error": "Rate limit exceeded"}),
        httpx.Response(500, json={"success": False, "error": "SCRAPE_ALL_ENGINES_FAILED"}),
        httpx.Response(200, json={"success": False, "error": "nope"}),
        httpx.Response(200, json={"success": True, "data": {"metadata": {"statusCode": 200}}}),
        httpx.Response(200, text="<html>not json</html>"),
    ],
)
def test_a_firecrawl_failure_on_the_homepage_keeps_the_plain_message(world, answer) -> None:
    world.firecrawl.pages[f"{SITE}/"] = answer
    error = _blocked(world)
    assert error is world.homepage
    assert world.firecrawl.scraped == [f"{SITE}/"]


def test_a_firecrawl_timeout_keeps_the_plain_message(world, monkeypatch) -> None:
    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(world.firecrawl, "handler", _timeout)
    assert _blocked(world) is world.homepage


def test_a_homepage_firecrawl_finds_missing_fails_like_the_browser_would(world) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(f"{SITE}/", "<html>gone</html>", status=404)
    error = _blocked(world)
    assert str(error) == http_error_message(404)


def test_a_homepage_redirected_to_another_site_fails_like_the_browser_would(world) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(
        f"{SITE}/", _page_html("Parked"), final_url="https://parking.example/"
    )
    with pytest.raises(CrawlerError, match="redirected outside the starting site"):
        _crawl(world)


# --- Internal pages ---------------------------------------------------------------------------


def test_an_internal_page_behind_the_check_stops_the_rest_like_the_browser_crawl(world) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(
        f"{SITE}/", _page_html("Acme", "/a/", "/b/", "/c/", "/d/")
    )
    world.firecrawl.pages[f"{SITE}/a"] = _scraped(f"{SITE}/a", "<html><body>a</body></html>")
    world.firecrawl.pages[f"{SITE}/b"] = _scraped(f"{SITE}/b", SG_CHALLENGE_PAGE)
    world.firecrawl.pages[f"{SITE}/c"] = _scraped(f"{SITE}/c", "<html>x</html>", status=404)

    result = _crawl(world)

    assert world.firecrawl.scraped == [f"{SITE}/", f"{SITE}/a", f"{SITE}/b"]
    assert [(page["url"], page["reason"]) for page in result.failed_pages] == [
        (f"{SITE}/b", BOT_CHECK_PAGE_REASON)
    ]
    assert [(page["url"], page["reason"]) for page in result.skipped_pages] == [
        (f"{SITE}/c", "stopped_after_bot_check"),
        (f"{SITE}/d", "stopped_after_bot_check"),
    ]
    assert result.status == "partial"


def test_internal_page_errors_get_short_reasons_and_the_crawl_goes_on(world) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(
        f"{SITE}/", _page_html("Acme", "/old/", "/portal/", "/flaky/", "/about/")
    )
    world.firecrawl.pages[f"{SITE}/old"] = _scraped(f"{SITE}/old", "<html>x</html>", status=404)
    world.firecrawl.pages[f"{SITE}/portal"] = _scraped(
        f"{SITE}/portal", "<html>x</html>", status=403
    )
    world.firecrawl.pages[f"{SITE}/flaky"] = httpx.Response(500, json={"success": False})

    result = _crawl(world)

    assert {page["url"]: page["reason"] for page in result.failed_pages} == {
        f"{SITE}/old": "Page not found (HTTP 404)",
        f"{SITE}/portal": "Refused by the site (HTTP 403)",
        f"{SITE}/flaky": PAGE_FETCH_FAILED_REASON,
    }
    assert [page.final_url for page in result.pages] == [f"{SITE}/", f"{SITE}/about"]
    assert result.skipped_pages == []


def test_out_of_credits_on_an_internal_page_stops_sending_more(world) -> None:
    world.firecrawl.pages[f"{SITE}/"] = _scraped(f"{SITE}/", _page_html("Acme", "/a/", "/b/"))
    world.firecrawl.pages[f"{SITE}/a"] = httpx.Response(402, json={"success": False})
    result = _crawl(world)
    assert world.firecrawl.scraped == [f"{SITE}/", f"{SITE}/a"]
    assert [page["reason"] for page in result.skipped_pages] == ["stopped_after_service_error"]


def test_no_new_page_is_started_after_the_time_budget(world, monkeypatch) -> None:
    monkeypatch.setattr(firecrawl_fallback, "_TOTAL_BUDGET_SECONDS", -1)
    result = _crawl(world)
    assert world.firecrawl.scraped == [f"{SITE}/"]
    assert [page["reason"] for page in result.skipped_pages] == ["time_budget_reached"]


def test_firecrawl_is_never_sent_a_private_address(world) -> None:
    # Local crawls may allow private hosts for our own browser; Firecrawl still gets none.
    settings = _settings(world, crawler_allow_private_hosts=True)
    with pytest.raises(SiteBlockedError) as excinfo:
        asyncio.run(crawler.crawl_site("http://localhost:8000/", settings, "job-1"))
    assert excinfo.value is world.homepage
    assert world.firecrawl.requests == []
    assert world.redis.counts == {}  # nothing sent, nothing counted


def test_an_internal_page_on_a_private_address_is_not_sent(world, monkeypatch) -> None:
    def _resolve(host: str):
        private = host == "acme-builders.example"
        return [ipaddress.ip_address("10.0.0.7" if private else "93.184.216.34")]

    monkeypatch.setattr(crawler, "_resolve_host_ips", _resolve)
    world.firecrawl.pages[f"{SITE}/"] = _scraped(
        f"{SITE}/", _page_html("Acme", "https://acme-builders.example/x/", "/about/")
    )
    result = _crawl(world)
    assert world.firecrawl.scraped == [f"{SITE}/", f"{SITE}/about"]
    assert [(page["url"], page["reason"]) for page in result.failed_pages] == [
        ("https://acme-builders.example/x", PAGE_FETCH_FAILED_REASON)
    ]


# --- Screenshots: same limits and checks as any download -----------------------------------------


def _stored_files(world) -> list:
    root = world.tmp_path / "screenshots"
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def _home_screenshot(world, screenshot: str | None = None, **overrides):
    answer = _scraped(f"{SITE}/", _page_html("Acme"))
    if screenshot is not None:
        answer["data"]["screenshot"] = screenshot
    world.firecrawl.pages[f"{SITE}/"] = answer
    return _crawl(world, crawler_firecrawl_max_pages=1, **overrides).pages[0]


@pytest.mark.parametrize(
    ("shot", "error"),
    [
        (httpx.Response(404), "screenshot download answered HTTP 404"),
        (httpx.Response(302, headers={"location": "http://169.254.169.254/"}), "HTTP 302"),
        (b"<html>not an image</html>", "screenshot is not a PNG or JPEG image"),
        (
            httpx.Response(200, content=PNG, headers={"content-length": str(50 * 1024 * 1024)}),
            "screenshot is too large",
        ),
    ],
)
def test_a_bad_screenshot_is_recorded_not_stored(world, shot, error) -> None:
    world.firecrawl.shot = shot
    page = _home_screenshot(world)
    assert page.screenshot_path is None
    assert error in (page.screenshot_error or "")
    assert _stored_files(world) == []


def test_a_streamed_screenshot_over_the_cap_is_dropped(world, monkeypatch) -> None:
    monkeypatch.setattr(firecrawl_fallback, "_SCREENSHOT_MAX_BYTES", 32)
    page = _home_screenshot(world)  # PNG is 72 bytes, sent without a length header
    assert page.screenshot_path is None
    assert page.screenshot_error == "screenshot is too large"


def test_a_screenshot_address_that_is_not_public_https_is_never_fetched(world, monkeypatch) -> None:
    def _resolve(host: str):
        return [ipaddress.ip_address("10.0.0.7" if host == SHOTS else "93.184.216.34")]

    monkeypatch.setattr(crawler, "_resolve_host_ips", _resolve)
    page = _home_screenshot(world)
    assert page.screenshot_error == "screenshot address is not a public host"
    page = _home_screenshot(world, f"http://{SHOTS}/media/shot.png")
    assert page.screenshot_error == "screenshot address is not https"
    assert not [r for r in world.firecrawl.requests if r.url.host == SHOTS]


def test_inline_and_jpeg_screenshots_are_stored_too(world) -> None:
    page = _home_screenshot(world, "data:image/png;base64," + base64.b64encode(PNG).decode())
    assert page.screenshot_error is None
    assert _stored_files(world) == [Path(page.screenshot_path)]
    assert Path(page.screenshot_path).read_bytes() == PNG

    world.firecrawl.shot = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    page = _home_screenshot(world)
    assert page.screenshot_path.endswith(".jpg")


def test_a_missing_screenshot_is_noted(world) -> None:
    answer = _scraped(f"{SITE}/", _page_html("Acme"))
    del answer["data"]["screenshot"]
    world.firecrawl.pages[f"{SITE}/"] = answer
    page = _crawl(world, crawler_firecrawl_max_pages=1).pages[0]
    assert page.screenshot_path is None
    assert page.screenshot_error == "no screenshot was returned"


# --- One log line per audit; the key never leaks ------------------------------------------------


def _fallback_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("firecrawl_")]


def test_one_log_line_per_fallback_audit(world, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _crawl(world)
    world.firecrawl.pages[f"{SITE}/"] = _scraped(f"{SITE}/", SG_CHALLENGE_PAGE)
    _blocked(world)
    _blocked(world, crawler_firecrawl_daily_limit=2)

    lines = _fallback_lines(caplog)
    assert len(lines) == 3
    assert "outcome=complete" in lines[0] and "pages=2" in lines[0]
    assert f"host={HOST}" in lines[0] and "blocked_by='SiteGround CAPTCHA'" in lines[0]
    assert "outcome=still_blocked" in lines[1] and "requests=1" in lines[1]
    assert "outcome=daily_limit_reached" in lines[2] and "requests=0" in lines[2]


def test_the_key_never_appears_in_logs_errors_or_stored_data(world, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    outputs: list[str] = []
    result = _crawl(world)
    outputs.append(json.dumps(result.to_dict()))
    outputs.extend(page.screenshot_error or "" for page in result.pages)

    world.firecrawl.pages[f"{SITE}/"] = httpx.Response(401, json={"error": f"bad key {KEY}"})
    outputs.append(str(_blocked(world)))
    world.firecrawl.pages[f"{SITE}/"] = _scraped(f"{SITE}/", SG_CHALLENGE_PAGE)
    outputs.append(str(_blocked(world)))

    outputs.append(caplog.text)
    outputs.append(repr(_settings(world)))
    assert all(KEY not in text for text in outputs)
    assert all(KEY not in str(request.url) for request in world.firecrawl.requests)
    assert all(KEY.encode() not in request.content for request in world.firecrawl.requests)


# --- Settings -------------------------------------------------------------------------------------


def test_firecrawl_settings_defaults_and_bounds() -> None:
    settings = Settings()
    assert Settings.model_fields["firecrawl_api_key"].default is None
    assert firecrawl_fallback.is_enabled(settings) is False
    assert settings.firecrawl_api_url == "https://api.firecrawl.dev"
    assert settings.crawler_firecrawl_max_pages == 10
    assert settings.crawler_firecrawl_daily_limit == 30
    assert Settings(firecrawl_api_url="https://fc.example/ ").firecrawl_api_url == (
        "https://fc.example"
    )
    for bad in (
        {"firecrawl_api_url": "api.firecrawl.dev"},
        {"crawler_firecrawl_max_pages": 0},
        {"crawler_firecrawl_max_pages": 26},
        {"crawler_firecrawl_daily_limit": -1},
        {"crawler_firecrawl_daily_limit": 1001},
    ):
        with pytest.raises(ValueError):
            Settings(**bad)
