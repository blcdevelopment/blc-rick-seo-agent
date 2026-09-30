import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.shared.audit_states import AuditStatus
from apps.shared.config import Settings
from apps.shared.models import AuditJob, Base
from apps.worker import tasks
from apps.worker.stages.crawler import (
    BOT_CHECK_BLOCKED_MESSAGE,
    CrawledPage,
    CrawlResult,
    RobotsPolicy,
    SiteBlockedError,
    http_error_message,
)


def _fake_crawler(url: str, settings: Settings, audit_id: str | None) -> CrawlResult:
    now = datetime.now(UTC).isoformat()
    page = CrawledPage(
        url=url,
        final_url="https://example.com/",
        status_code=200,
        title="Example Builder",
        html="""
        <html>
          <head>
            <title>Example Builder Website</title>
            <meta name="description" content="Custom builder serving local homeowners." />
          </head>
          <body>
            <h1>Example Builder</h1>
            <a class="btn cta" href="/estimate">Request Estimate</a>
            <img src="/home.jpg" alt="Finished custom home" />
          </body>
        </html>
        """,
        text="Example Builder Request Estimate",
        fetched_at=now,
    )
    return CrawlResult(
        requested_url=url,
        start_url=url,
        final_url="https://example.com/",
        status="complete",
        pages=[page],
        discovered_links=[],
        skipped_pages=[],
        failed_pages=[],
        robots=RobotsPolicy(status="disabled", robots_url=None),
        started_at=now,
        completed_at=now,
        max_pages=settings.crawler_max_pages,
        user_agent=settings.crawler_user_agent,
    )


def _fake_psi(urls: list[str], settings: Settings) -> dict:
    return {
        "status": "skipped",
        "reason": "unit_test",
        "homepage_url": urls[0],
        "pages_requested": len(urls),
        "pages_analyzed": 0,
        "pages": [],
        "strategies": {},
    }


def test_run_collection_audit_persists_epic_4_artifacts(monkeypatch, tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(tasks, "SessionLocal", TestingSession)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            google_psi_api_key=None,
            crawler_screenshots_enabled=False,
            # Keep the unit test network-free: the external SEO stage takes its
            # real skip paths (same contract the hermetic QA harness pins).
            site_health_enabled=False,
            screaming_frog_enabled=False,
            google_oauth_client_id="",
        ),
    )
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% unit test placeholder\n")

    def fake_render_audit_pdf(job, result, settings):
        return tasks.PdfRenderResult(
            pdf_path=str(pdf_path),
            report_metadata={
                "status": "complete",
                "renderer": "weasyprint",
                "renderer_version": "unit-test",
                "report_payload_version": "phase1-report-v1",
                "generated_at": "2026-06-01T00:00:00+00:00",
                "pdf_path": str(pdf_path),
                "pdf_size_bytes": pdf_path.stat().st_size,
                "page_count": 1,
                "storage": {"type": "local_filesystem", "directory": str(tmp_path)},
            },
            page_count=1,
            size_bytes=pdf_path.stat().st_size,
        )

    monkeypatch.setattr(tasks, "render_audit_pdf", fake_render_audit_pdf)

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            status=AuditStatus.QUEUED.value,
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    tasks.run_collection_audit(job_id, crawler=_fake_crawler, psi_collector=_fake_psi)

    with TestingSession() as db:
        job = db.get(AuditJob, job_id)
        assert job is not None
        assert job.status == AuditStatus.COMPLETE.value
        assert job.current_stage == "Audit report complete"
        assert job.progress_pct == 100
        assert job.result is not None
        assert job.result.crawled_pages["summary"]["successful_pages"] == 1
        assert job.result.psi_facts["status"] == "skipped"
        assert job.result.psi_facts["pages_requested"] == 1
        assert job.result.seo_facts["pages_analyzed"] == 1
        assert job.result.uxui_facts["summary"]["total_ctas"] == 1
        assert job.result.seo_score > 0
        assert job.result.uxui_score > 0
        assert job.result.lead_gen_score > 0
        assert job.result.score_breakdown["status"] == "complete"
        assert job.result.external_seo_facts["sources"]["technical_crawl"] == "skipped"
        assert job.result.external_seo_facts["gsc"]["status"] == "skipped"
        assert job.result.commentary["status"] == "deterministic"
        assert job.result.commentary["provider"] == "deterministic"
        assert job.result.validation_log["status"] == "complete"
        assert job.result.pdf_path == str(pdf_path)
        assert job.result.report_metadata["renderer"] == "weasyprint"


def _fake_crawler_with_axe(url: str, settings: Settings, audit_id: str | None) -> CrawlResult:
    """Same as _fake_crawler but with a raw axe-core result attached to the page, so the advisory
    normalize step has something to produce when the pass is enabled."""
    result = _fake_crawler(url, settings, audit_id)
    axe = {
        "violations": [
            {
                "id": "color-contrast",
                "impact": "serious",
                "help": "Elements must meet contrast thresholds",
                "helpUrl": "https://example.org/contrast",
                "tags": ["wcag2aa", "wcag143"],
                "nodes": [{"target": [".cta"], "failureSummary": "Fix the contrast ratio."}],
            }
        ],
        "incomplete": [],
    }
    result.pages[0] = replace(result.pages[0], axe_results=axe)
    return result


def test_accessibility_advisory_never_changes_scores(monkeypatch, tmp_path) -> None:
    """The load-bearing guarantee: running the opt-in advisory pass produces byte-for-byte
    identical scores and score_breakdown vs. not running it; it only fills the separate
    accessibility_facts column."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(tasks, "SessionLocal", TestingSession)

    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% unit test placeholder\n")
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

    def _settings(enabled: bool) -> Settings:
        return Settings(
            _env_file=None,
            google_psi_api_key=None,
            crawler_screenshots_enabled=False,
            site_health_enabled=False,
            screaming_frog_enabled=False,
            google_oauth_client_id="",
            accessibility_advisory_enabled=enabled,
            # Path is irrelevant here — the fake crawler injects axe_results directly, so
            # run_axe_on_page is never called.
            accessibility_axe_script_path=tmp_path / "unused.js",
        )

    def _run(enabled: bool) -> dict:
        monkeypatch.setattr(tasks, "get_settings", lambda: _settings(enabled))
        with TestingSession() as db:
            job = AuditJob(
                url="https://example.com/",
                status=AuditStatus.QUEUED.value,
                current_stage="Queued",
                progress_pct=0,
            )
            db.add(job)
            db.commit()
            job_id = str(job.id)
        tasks.run_collection_audit(job_id, crawler=_fake_crawler_with_axe, psi_collector=_fake_psi)
        with TestingSession() as db:
            result = db.get(AuditJob, job_id).result
            return {
                "scores": (result.seo_score, result.uxui_score, result.lead_gen_score),
                "breakdown": json.dumps(result.score_breakdown, sort_keys=True),
                "a11y": result.accessibility_facts,
            }

    off = _run(False)
    on = _run(True)

    # Scores + full breakdown are identical whether or not the advisory pass ran.
    assert off["scores"] == on["scores"]
    assert off["breakdown"] == on["breakdown"]
    # Disabled => NULL column / no section; enabled => populated advisory bundle.
    assert off["a11y"] is None
    assert on["a11y"] is not None
    assert on["a11y"]["status"] == "complete"
    assert any(issue["rule_id"] == "color-contrast" for issue in on["a11y"]["issues"])


def _run_failed_audit(monkeypatch, crawler, psi_collector=_fake_psi) -> SimpleNamespace:
    """Run the pipeline with `crawler` and return what was stored (the task re-raises)."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(tasks, "SessionLocal", TestingSession)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            google_psi_api_key=None,
            crawler_screenshots_enabled=False,
            site_health_enabled=False,
            screaming_frog_enabled=False,
            google_oauth_client_id="",
        ),
    )
    with TestingSession() as db:
        job = AuditJob(
            url="https://www.example.com/",
            status=AuditStatus.QUEUED.value,
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    with pytest.raises(SiteBlockedError):
        tasks.run_collection_audit(job_id, crawler=crawler, psi_collector=psi_collector)

    with TestingSession() as db:
        stored = db.get(AuditJob, job_id)
        assert stored is not None
        return SimpleNamespace(
            status=stored.status,
            current_stage=stored.current_stage,
            error_message=stored.error_message,
            result=stored.result,
        )


def test_site_blocked_by_a_bot_check_fails_with_the_plain_message(monkeypatch) -> None:
    # A homepage the site's bot check never let through fails the whole audit, and the visitor
    # reads exactly the plain text under "Audit failed." (no "Could not render ..." wrapper).
    def _blocked_crawler(url: str, settings: Settings, audit_id: str | None) -> CrawlResult:
        raise SiteBlockedError(BOT_CHECK_BLOCKED_MESSAGE)

    job = _run_failed_audit(monkeypatch, _blocked_crawler)

    assert job.status == AuditStatus.FAILED.value
    assert job.current_stage == "Audit collection failed"
    assert job.error_message == BOT_CHECK_BLOCKED_MESSAGE
    assert job.result is None


def test_site_refusing_the_scanner_fails_with_the_plain_http_message(monkeypatch) -> None:
    def _refused_crawler(url: str, settings: Settings, audit_id: str | None) -> CrawlResult:
        raise SiteBlockedError(http_error_message(403))

    job = _run_failed_audit(monkeypatch, _refused_crawler)

    assert job.status == AuditStatus.FAILED.value
    assert job.error_message == http_error_message(403)
    assert "Could not render" not in (job.error_message or "")


def test_a_bot_check_page_is_never_scored_or_sent_to_pagespeed(monkeypatch) -> None:
    # Second guard behind the crawler: a crawl that still ended on SiteGround's challenge URL
    # fails plainly, before PageSpeed is asked about it.
    challenge = "https://www.example.com/.well-known/sgcaptcha/?r=%2F&y=ipc:203.0.113.9:1790749657"
    psi_calls: list[list[str]] = []

    def _challenge_crawler(url: str, settings: Settings, audit_id: str | None) -> CrawlResult:
        result = _fake_crawler(url, settings, audit_id)
        result.pages[0] = replace(result.pages[0], final_url=challenge, status_code=202)
        result.final_url = challenge
        return result

    def _recording_psi(urls: list[str], settings: Settings) -> dict:
        psi_calls.append(list(urls))
        return _fake_psi(urls, settings)

    job = _run_failed_audit(monkeypatch, _challenge_crawler, _recording_psi)

    assert job.status == AuditStatus.FAILED.value
    assert job.error_message == BOT_CHECK_BLOCKED_MESSAGE
    assert psi_calls == []


def test_psi_page_urls_never_include_a_bot_check_page() -> None:
    settings = Settings(_env_file=None)
    result = _fake_crawler("https://example.com/", settings, None)
    child = replace(
        result.pages[0],
        url="https://example.com/about",
        final_url="https://example.com/.well-known/captcha/?r=%2Fabout",
    )
    result.pages.append(child)
    assert tasks._psi_page_urls(result, "https://example.com/") == ["https://example.com/"]

    # Nothing usable left: fall back to the address the visitor submitted.
    result.pages = [child]
    assert tasks._psi_page_urls(result, "https://example.com/") == ["https://example.com/"]
