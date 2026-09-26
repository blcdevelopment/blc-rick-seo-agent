"""Combined audit: website pipeline (untouched) -> social audit -> ONE report with the social
section + Overall Lead-Gen Readiness score appended at the end.

Covers the overall-readiness weighting, the end-to-end combined orchestrator (real scoring + real
PDF render so the appended template sections are exercised), and the load-bearing invariant that a
website-only audit is unchanged (no social/overall data, byte-identical report payload shape).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.schemas.audits import AuditCreateRequest
from apps.shared.config import Settings
from apps.shared.models import AuditJob, Base
from apps.worker import tasks
from apps.worker.stages.crawler import CrawledPage, CrawlResult, RobotsPolicy
from apps.worker.stages.docx_renderer import render_audit_docx
from apps.worker.stages.report_payload import compose_report_payload
from apps.worker.stages.scoring import compose_overall_readiness_score
from apps.worker.stages.social.extractor import extract_social_facts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 6, 23, tzinfo=UTC)


# --------------------------------------------------------------------------- weighting (pure)
def _settings() -> Settings:
    return Settings(_env_file=None)


def test_overall_readiness_blends_website_and_social_70_30() -> None:
    out = compose_overall_readiness_score(
        website_lead_gen=80, social_score=60, settings=_settings()
    )
    assert out["status"] == "complete"
    # 80*0.70 + 60*0.30 = 74, half-up rounded.
    assert out["score"] == 74
    assert out["band"] == "fair"
    assert out["weights"] == {"website": 0.70, "social": 0.30}
    assert out["inputs"] == {"website_lead_gen": 80, "social": 60}


def test_overall_readiness_rescales_to_website_when_social_missing() -> None:
    out = compose_overall_readiness_score(
        website_lead_gen=82, social_score=None, settings=_settings()
    )
    assert out["status"] == "website_only"
    assert out["score"] == 82
    assert out["weights"] == {"website": 1.0, "social": 0.0}


def test_overall_readiness_is_skipped_without_website_score() -> None:
    out = compose_overall_readiness_score(
        website_lead_gen=None, social_score=70, settings=_settings()
    )
    assert out["status"] == "skipped"
    assert out["score"] is None
    assert out["band"] == "unknown"


# --------------------------------------------------------------------------- request validation
def test_combined_request_requires_both_url_and_a_handle() -> None:
    with pytest.raises(ValidationError):
        AuditCreateRequest(audit_type="combined", url="https://example.com")  # no handle
    with pytest.raises(ValidationError):
        AuditCreateRequest(audit_type="combined", social_handles={"instagram": "acme"})  # no url
    req = AuditCreateRequest(
        audit_type="combined", url="https://example.com", social_handles={"instagram": "acme"}
    )
    assert req.audit_type == "combined"


# --------------------------------------------------------------------------- orchestrator (e2e)
def _fake_crawler(url: str, settings: Settings, audit_id: str | None) -> CrawlResult:
    now = datetime.now(UTC).isoformat()
    page = CrawledPage(
        url=url,
        final_url="https://example.com/",
        status_code=200,
        title="Example Builder",
        html="""
        <html><head>
          <title>Example Builder Website</title>
          <meta name="description" content="Custom builder serving local homeowners." />
        </head><body>
          <h1>Example Builder</h1>
          <a class="btn cta" href="/estimate">Request Estimate</a>
          <img src="/home.jpg" alt="Finished custom home" />
        </body></html>
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


def _session(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _patch_settings(monkeypatch, TestingSession, tmp_path, **overrides) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", TestingSession)
    kwargs = dict(
        _env_file=None,
        google_psi_api_key=None,
        crawler_screenshots_enabled=False,
        site_health_enabled=False,
        screaming_frog_enabled=False,
        google_oauth_client_id="",
        local_report_storage_dir=tmp_path / "reports",
        # A configured Apify token so auto-discovery's credential gate lets a discovered website
        # audit promote to combined (tests inject a fake collector for the actual social data).
        apify_api_token="test-apify-token",
    )
    kwargs.update(overrides)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(**kwargs))


def test_combined_audit_renders_one_report_with_social_and_overall(tmp_path, monkeypatch) -> None:
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    strong = json.loads((FIXTURES / "social_instagram_strong.json").read_text())

    def fake_collector(settings, handles):
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acme", "raw": strong}], now=NOW
        )

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="combined",
            social_handles={"instagram": "acme"},
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    # Real scoring + real WeasyPrint render so the appended template sections are exercised.
    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=fake_collector
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.progress_pct == 100
        result = job.result
        # Website scores intact (the website pipeline is untouched).
        assert result.seo_score is not None
        assert result.uxui_score is not None
        assert result.lead_gen_score is not None
        # Social merged onto the SAME result.
        assert result.social_score is not None and result.social_score >= 80
        overall = result.score_breakdown["overall_readiness"]
        assert overall["status"] == "complete"
        expected = int(result.lead_gen_score * 0.70 + result.social_score * 0.30 + 0.5)
        assert overall["score"] == expected
        # The composed report appends both sections.
        payload = compose_report_payload(job, result)
        assert payload.social_audit is not None
        assert payload.social_audit["score"] == result.social_score
        assert payload.overall_readiness is not None
        assert payload.overall_readiness["score"] == expected
        # ONE combined PDF was rendered to local storage.
        assert result.pdf_path and Path(result.pdf_path).exists()
        assert Path(result.pdf_path).read_bytes().startswith(b"%PDF")
        # The on-demand DOCX appends the same social + overall sections after the website content.
        docx_res = render_audit_docx(
            job, result, Settings(_env_file=None, local_report_storage_dir=tmp_path / "reports")
        )
        with ZipFile(docx_res.docx_path) as archive:
            doc_xml = archive.read("word/document.xml").decode("utf-8")
        # Combined audits are titled for both surfaces (the website-only DOCX keeps the
        # original title — covered by the website-unchanged invariant test).
        assert "Website &amp; Social Media Audit Report" in doc_xml
        assert "Social Media Audit" in doc_xml
        assert "Overall Lead-Gen Readiness" in doc_xml


def test_combined_audit_degrades_to_website_when_social_fails(tmp_path, monkeypatch) -> None:
    # Graceful degradation: a failure in the optional social add-on must NOT fail the whole
    # combined audit — the already-scored website report still completes (no social/overall).
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def boom_collector(settings, handles):
        raise RuntimeError("apify exploded mid-collection")

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="combined",
            social_handles={"instagram": "acme"},
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=boom_collector
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"  # NOT failed
        result = job.result
        assert result.seo_score is not None and result.lead_gen_score is not None
        assert result.social_score is None
        assert "overall_readiness" not in (result.score_breakdown or {})
        assert result.pdf_path and Path(result.pdf_path).exists()


def test_explicit_combined_audit_merges_failed_social_collection(tmp_path, monkeypatch) -> None:
    # The operator explicitly asked for a combined audit, so a collection that came back with no
    # usable profile data is still merged: the failed-status social section is informative, and
    # the overall headline degrades to the website-only rescale.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def failed_collector(settings, handles):
        # Provider fetch returned nothing (raw None) -> a failed-status bundle, no score.
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acme", "raw": None}], now=NOW
        )

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="combined",
            social_handles={"instagram": "acme"},
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=failed_collector
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.audit_type == "combined"  # explicit type is kept
        result = job.result
        assert result.social_score is None
        assert result.social_facts["status"] == "failed"
        assert result.score_breakdown["social"]["status"] == "failed"
        assert result.score_breakdown["overall_readiness"]["status"] == "website_only"
        assert result.score_breakdown["overall_readiness"]["score"] == result.lead_gen_score
        # The cover must not promise social content: the website_only overall carries a
        # real score, but the report has no social data behind it.
        from pypdf import PdfReader

        cover_text = " ".join(PdfReader(result.pdf_path).pages[0].extract_text().split())
        assert "Website Audit Report" in cover_text
        assert "Website & Social Media Audit Report" not in cover_text
        assert "Overall Lead-Gen Readiness" not in cover_text


def test_rerun_enrichment_preserves_combined_sections(tmp_path, monkeypatch) -> None:
    # Re-running external SEO enrichment on a combined audit must keep the social breakdown and
    # recompute the Overall Lead-Gen Readiness, not silently drop them.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    strong = json.loads((FIXTURES / "social_instagram_strong.json").read_text())

    def fake_collector(settings, handles):
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acme", "raw": strong}], now=NOW
        )

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="combined",
            social_handles={"instagram": "acme"},
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=fake_collector
    )
    # A benchmark can be present alongside social/overall; it is presentation-only and, like them,
    # is not re-collected on enrichment — so the rerun must re-attach it, not drop it.
    benchmark_blob = {
        "status": "complete",
        "provider": "semrush",
        "target_url": "https://example.com/",
        "niche": None,
        "competitors": [
            {
                "label": "rival.com",
                "is_industry": False,
                "source": "semrush",
                "seo": 60,
                "uxui": 70,
                "lead_gen": 65,
                "social": 40,
                "overall": 55,
            }
        ],
    }
    with TestingSession() as db:
        result = db.get(AuditJob, UUID(job_id)).result
        before_overall = result.score_breakdown["overall_readiness"]["score"]
        before_social = result.social_score
        result.score_breakdown = {**result.score_breakdown, "benchmark": benchmark_blob}
        db.commit()

    # Rerun external enrichment (runs synchronously). External sources all skip in this config,
    # so the website score is unchanged — but the combined sections must survive.
    tasks.rerun_external_enrichment_for_audit(job_id)

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        result = job.result
        assert result.social_score == before_social
        assert isinstance(result.score_breakdown.get("social"), dict)
        overall = result.score_breakdown.get("overall_readiness")
        assert overall is not None and overall["score"] == before_overall
        # The benchmark section is re-attached, not silently dropped by the website-only rescore.
        assert result.score_breakdown.get("benchmark") == benchmark_blob


def test_rerun_enrichment_does_not_fabricate_overall_readiness(tmp_path, monkeypatch) -> None:
    # A combined-typed job whose social step never produced data (collector crashed, so the
    # result carries no social score and no overall_readiness key) must stay website-only on
    # rerun — the enrichment rerun must not invent an Overall Readiness the audit never had.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def boom_collector(settings, handles):
        raise RuntimeError("apify exploded mid-collection")

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="combined",
            social_handles={"instagram": "acme"},
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=boom_collector
    )

    with TestingSession() as db:
        result = db.get(AuditJob, UUID(job_id)).result
        assert result.social_score is None
        assert "overall_readiness" not in (result.score_breakdown or {})

    tasks.rerun_external_enrichment_for_audit(job_id)

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        result = job.result
        assert result.social_score is None
        assert "overall_readiness" not in (result.score_breakdown or {})
        assert "social" not in (result.score_breakdown or {})


def _fake_crawler_with_social_footer(
    url: str, settings: Settings, audit_id: str | None
) -> CrawlResult:
    now = datetime.now(UTC).isoformat()
    page = CrawledPage(
        url=url,
        final_url="https://example.com/",
        status_code=200,
        title="Example Builder",
        html="""
        <html><head>
          <title>Example Builder Website</title>
          <meta name="description" content="Custom builder serving local homeowners." />
        </head><body>
          <h1>Example Builder</h1>
          <a class="btn cta" href="/estimate">Request Estimate</a>
          <img src="/home.jpg" alt="Finished custom home" />
          <footer class="site-footer">
            <a href="https://www.instagram.com/acmebuilders/">Instagram</a>
          </footer>
        </body></html>
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


def test_website_audit_with_footer_social_is_promoted_to_combined(tmp_path, monkeypatch) -> None:
    # Auto-discovery: a plain website audit whose page links to a social profile becomes a combined
    # audit (handles back-filled, audit_type promoted, social + overall sections appended).
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    strong = json.loads((FIXTURES / "social_instagram_strong.json").read_text())
    seen_handles: dict[str, str] = {}

    def fake_collector(settings, handles):
        seen_handles.update(handles or {})
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acmebuilders", "raw": strong}], now=NOW
        )

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="website",  # operator gave NO social handles
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id,
        crawler=_fake_crawler_with_social_footer,
        psi_collector=_fake_psi,
        social_collector=fake_collector,
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        # Promoted: discovered the footer Instagram link and back-filled it onto the job.
        assert job.audit_type == "combined"
        assert job.social_handles == {"instagram": "https://www.instagram.com/acmebuilders/"}
        # The discovered handle was the one handed to the social collector.
        assert seen_handles == {"instagram": "https://www.instagram.com/acmebuilders/"}
        result = job.result
        assert result.seo_score is not None and result.lead_gen_score is not None
        assert result.social_score is not None
        overall = result.score_breakdown.get("overall_readiness")
        assert overall is not None and overall["status"] == "complete"
        payload = compose_report_payload(job, result)
        assert payload.social_audit is not None
        assert payload.overall_readiness is not None


def test_website_audit_not_promoted_when_social_collection_returns_nothing(
    tmp_path, monkeypatch
) -> None:
    # Auto-promotion is gated on collection SUCCESS: discovery found the footer Instagram link and
    # a credential is configured, but the collection came back with no usable profile data (raw
    # fetch None -> failed status, no score). The website audit must stay byte-identical: no
    # handle write, no type flip, no social/overall breakdown keys, no appended report sections.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def failed_collector(settings, handles):
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acmebuilders", "raw": None}], now=NOW
        )

    def boom_places(settings, *, query, expected_url=None):
        raise AssertionError(
            "Places enrichment must not run when an auto-discovered collection failed — "
            "the result is discarded one step later, so the billed call is pure waste"
        )

    monkeypatch.setattr(tasks, "collect_google_business_facts", boom_places)

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="website",  # operator gave NO social handles
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id,
        crawler=_fake_crawler_with_social_footer,
        psi_collector=_fake_psi,
        social_collector=failed_collector,
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.audit_type == "website"  # NOT promoted
        assert job.social_handles in (None, {})
        result = job.result
        assert result.seo_score is not None and result.lead_gen_score is not None
        assert result.social_score is None
        assert result.social_facts in (None, {})
        assert "social" not in (result.score_breakdown or {})
        assert "overall_readiness" not in (result.score_breakdown or {})
        payload = compose_report_payload(job, result)
        assert payload.social_audit is None
        assert payload.overall_readiness is None


def test_website_audit_with_no_social_links_stays_website(tmp_path, monkeypatch) -> None:
    # Auto-discovery on, but the page links to no social profiles -> no social step, stays a plain
    # website audit (byte-identical behaviour). The collector must never run.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def boom_collector(settings, handles):
        raise AssertionError("social collector must NOT run when no social links are found")

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="website",
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    # _fake_crawler's HTML has no social links.
    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=boom_collector
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.audit_type == "website"
        assert job.social_handles in (None, {})
        assert job.result.social_score is None
        assert "overall_readiness" not in (job.result.score_breakdown or {})


def test_website_audit_survives_social_discovery_error(tmp_path, monkeypatch) -> None:
    # Exception isolation: a crash in the optional auto-discovery step must NOT fail the already-
    # scored website audit (discovery runs after the website result is committed). The audit
    # completes as a plain website report and the social collector is never reached.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def boom_discovery(pages, **kwargs):
        raise RuntimeError("discovery parser blew up")

    monkeypatch.setattr(tasks, "discover_social_links", boom_discovery)

    def boom_collector(settings, handles):
        raise AssertionError("social collector must NOT run when discovery fails with no handles")

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="website",
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    # The crawled page has a footer Instagram link, so discovery WOULD run — but it raises.
    tasks.run_collection_audit(
        job_id,
        crawler=_fake_crawler_with_social_footer,
        psi_collector=_fake_psi,
        social_collector=boom_collector,
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"  # NOT failed
        assert job.audit_type == "website"
        assert job.result.social_score is None
        assert "overall_readiness" not in (job.result.score_breakdown or {})
        assert job.result.pdf_path and Path(job.result.pdf_path).exists()


def test_website_with_social_link_but_no_token_stays_website(tmp_path, monkeypatch) -> None:
    # Credential gate: auto-discovery finds the footer Instagram link, but with NO Apify token the
    # social collection can't return data — so the audit must NOT be promoted to combined (no hollow
    # social/overall sections) and the collector must never run.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path, apify_api_token=None)

    def boom_collector(settings, handles):
        raise AssertionError("social collector must NOT run without a usable provider credential")

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="website",
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    # The crawled page HAS a footer Instagram link (discovery would find it) — but there's no token.
    tasks.run_collection_audit(
        job_id,
        crawler=_fake_crawler_with_social_footer,
        psi_collector=_fake_psi,
        social_collector=boom_collector,
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.audit_type == "website"  # NOT promoted
        assert job.social_handles in (None, {})
        assert job.result.social_score is None
        assert "overall_readiness" not in (job.result.score_breakdown or {})


def test_website_audit_is_unchanged_by_combined_feature(tmp_path, monkeypatch) -> None:
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def boom_collector(settings, handles):
        raise AssertionError("social collector must NOT run for a plain website audit")

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="website",
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=boom_collector
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        result = job.result
        assert result.social_score is None
        assert "overall_readiness" not in (result.score_breakdown or {})
        payload = compose_report_payload(job, result)
        assert payload.social_audit is None
        assert payload.overall_readiness is None


# ----------------------------------------------------------------- malformed stored handles
def test_explicit_social_handles_tolerates_malformed_stored_value() -> None:
    # Stored JSON is untrusted: a non-dict social_handles must yield {} instead of raising.
    job = SimpleNamespace(social_handles=["instagram", "acme"])
    assert tasks._explicit_social_handles(job) == {}
    assert tasks._explicit_social_handles(SimpleNamespace(social_handles="acme")) == {}
    assert tasks._explicit_social_handles(SimpleNamespace(social_handles=None)) == {}


def test_resolve_social_handles_safely_tolerates_malformed_stored_value() -> None:
    # The safety wrapper's fallback path re-calls _explicit_social_handles, so a malformed stored
    # value must resolve to {} (no raise) rather than crashing the committed website audit.
    settings = _settings()
    job = SimpleNamespace(social_handles=["instagram"], url="https://example.com/")
    crawl_result = _fake_crawler("https://example.com/", settings, None)
    assert tasks._resolve_social_handles_safely(job, crawl_result, settings) == {}


def test_explicit_combined_failed_collection_skips_places_lookup(tmp_path, monkeypatch) -> None:
    # The billed Places enrichment is gated on a USABLE collection for BOTH paths: an
    # explicitly combined audit whose social collection FAILED merges the honest failure note
    # alone — no billed lookup, and no google_business block sitting in failed-status facts
    # for a render surface to leak while the PDF/DOCX suppress the social body.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)

    def failed_collector(settings, handles):
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acmebuilders", "raw": None}], now=NOW
        )

    def boom_places(settings, *, query, expected_url=None):
        raise AssertionError("Places must not run over a failed social collection")

    monkeypatch.setattr(tasks, "collect_google_business_facts", boom_places)

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            audit_type="combined",
            social_handles={"instagram": "acmebuilders"},
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(
        job_id,
        crawler=_fake_crawler,
        psi_collector=_fake_psi,
        social_collector=failed_collector,
    )

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        result = job.result
        assert result.social_score is None
        social_facts = result.social_facts or {}
        assert social_facts.get("status") == "failed"
        assert "google_business" not in social_facts


# ------------------------------------------------------------------- AI Visibility auto-run
def _fake_ai_visibility(settings, *, domain, retrieved_at):
    return {
        "status": "complete",
        "provider": "semrush",
        "domain": domain,
        "retrieved_at": retrieved_at,
        "visibility_score": 19,
        "visibility_band": "Low",
        "mentions": 28,
        "citations": 380,
        "cited_pages": 42,
        "share_of_voice_pct": 12.5,
        "per_platform": [{"platform": "AI Overview", "mentions": 22, "share_pct": 78.6}],
        "topics": [],
        "competitors": [],
        "by_country": [],
    }


def test_ai_visibility_auto_runs_and_appends_section_when_enabled(tmp_path, monkeypatch) -> None:
    # With the feature enabled, every website audit fetches AI visibility (bot/vision mocked here)
    # and the section lands in the report — no on-demand button needed.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path, ai_visibility_enabled=True)
    monkeypatch.setattr(tasks, "collect_ai_visibility_facts", _fake_ai_visibility)

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/", status="queued", current_stage="Queued", progress_pct=0
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(job_id, crawler=_fake_crawler, psi_collector=_fake_psi)

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        result = job.result
        aiv = result.score_breakdown["ai_visibility"]
        assert aiv["visibility_score"] == 19
        assert aiv["domain"] == "example.com"  # derived from the audited URL, not hardcoded
        # Website scores are untouched — AI visibility is presentation only.
        assert result.seo_score is not None and result.lead_gen_score is not None
        payload = compose_report_payload(job, result)
        assert payload.ai_visibility is not None
        assert payload.ai_visibility["visibility_score"] == 19


def test_ai_visibility_absent_when_disabled_by_default(tmp_path, monkeypatch) -> None:
    # Default (disabled) => no fetch, no section, byte-identical to before the feature existed.
    TestingSession = _session(tmp_path)
    _patch_settings(monkeypatch, TestingSession, tmp_path)  # ai_visibility_enabled defaults False

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/", status="queued", current_stage="Queued", progress_pct=0
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    tasks.run_collection_audit(job_id, crawler=_fake_crawler, psi_collector=_fake_psi)

    with TestingSession() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert "ai_visibility" not in (job.result.score_breakdown or {})
