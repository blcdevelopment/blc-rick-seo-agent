"""SOCIAL_AUDITS_ENABLED=false (the Rick edition) takes social media out of the audit: the API
refuses social handles, the worker never discovers or collects social data for any job (a job
queued before the switch included), and the report has no social section and no social or overall
score. The code default (on) is the parent's behaviour, which the existing social/combined tests
keep proving. Audits completed before the switch keep what they already have."""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from zipfile import ZipFile

import pytest
import yaml
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api import auth
from apps.api.deps import get_db_session
from apps.api.main import app
from apps.api.routes import audits as audit_routes
from apps.api.schemas import audits as audit_schemas
from apps.shared.config import SOCIAL_AUDITS_DISABLED_MESSAGE, Settings
from apps.shared.models import AuditJob, Base
from apps.worker import tasks
from apps.worker.stages.crawler import CrawledPage, CrawlResult, RobotsPolicy
from apps.worker.stages.report_payload import compose_report_payload
from apps.worker.stages.social import places_provider, providers
from apps.worker.stages.social.extractor import extract_social_facts
from tests.unit.test_combined_audit import (
    FIXTURES,
    NOW,
    _fake_crawler,
    _fake_psi,
    _patch_settings,
    _session,
)

ROOT = Path(__file__).resolve().parents[2]
SITE = "https://prospect.example"
# Every provider credential configured, so only the switch can keep the social step from running.
SOCIAL_KEYS = {
    "apify_api_token": "test-apify-token",
    "youtube_api_key": "test-youtube-key",
    "google_places_api_key": "test-places-key",
}
# Text that only the social or overall parts of a report print.
SOCIAL_REPORT_TEXT = (
    "Social Media Audit",
    "Social Score",
    "Overall Lead-Gen Readiness",
    "Website & Social Media Audit Report",
)


# ------------------------------------------------------------------------------------------ API
@pytest.fixture
def client_for(monkeypatch) -> Generator[Any, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    def build(**overrides: Any) -> tuple[TestClient, sessionmaker]:
        settings = Settings(_env_file=None, audit_enqueue_enabled=False, **overrides)
        monkeypatch.setattr(auth, "get_settings", lambda: settings)
        monkeypatch.setattr(audit_routes, "get_settings", lambda: settings)
        monkeypatch.setattr(audit_schemas, "get_settings", lambda: settings)
        return TestClient(app), factory

    app.dependency_overrides[get_db_session] = override_db
    yield build
    app.dependency_overrides.clear()


def test_code_default_keeps_the_parents_social_audits() -> None:
    assert Settings(_env_file=None).social_audits_enabled is True


@pytest.mark.parametrize("public", [True, False])
def test_off_refuses_any_request_that_carries_social_handles(client_for, public) -> None:
    client, factory = client_for(social_audits_enabled=False, public_audits_enabled=public)
    requests = [
        {"url": SITE, "social_handles": {"instagram": "acme"}},
        {"url": SITE, "audit_type": "combined", "social_handles": {"youtube": "@acme"}},
        # Refused with the same plain message in public mode too, not public mode's "social
        # links are optional" one.
        {"audit_type": "social", "social_handles": {"facebook": "acme"}},
        # A social or combined request without handles (or without a URL) gets the same plain
        # message, not a request to add the handle the next try would be refused for.
        {"url": SITE, "audit_type": "combined"},
        {"audit_type": "combined"},
        {"audit_type": "social"},
    ]
    for body in requests:
        response = client.post("/audits", json=body)
        assert response.status_code == 422, body
        assert response.json()["detail"] == SOCIAL_AUDITS_DISABLED_MESSAGE
    with factory() as db:
        assert db.scalars(select(AuditJob)).all() == []


def test_off_accepts_a_plain_website_audit(client_for) -> None:
    client, factory = client_for(social_audits_enabled=False, public_audits_enabled=True)
    assert client.post("/audits", json={"url": SITE}).status_code == 201
    # A blank value is no handle (the API drops blank handles everywhere else too).
    blank = client.post("/audits", json={"url": SITE, "social_handles": {"instagram": ""}})
    assert blank.status_code == 201
    with factory() as db:
        jobs = db.scalars(select(AuditJob)).all()
        assert [(job.audit_type, job.social_handles) for job in jobs] == [("website", None)] * 2


def test_on_still_creates_combined_and_social_audits(client_for) -> None:
    client, factory = client_for()
    combined = client.post(
        "/audits",
        json={"url": SITE, "audit_type": "combined", "social_handles": {"instagram": "acme"}},
    )
    social = client.post(
        "/audits", json={"audit_type": "social", "social_handles": {"instagram": "acme"}}
    )
    assert combined.status_code == social.status_code == 201
    with factory() as db:
        job = db.get(AuditJob, UUID(combined.json()["job_id"]))
        assert (job.audit_type, job.social_handles) == ("combined", {"instagram": "acme"})
    # The request checks are unchanged: a combined audit still needs a handle.
    no_handle = client.post("/audits", json={"url": SITE, "audit_type": "combined"})
    assert no_handle.status_code == 422
    assert "at least one social handle is required" in no_handle.text


# --------------------------------------------------------------------------------------- worker
def _crawler_linking_instagram_and_youtube(
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
            <a href="https://www.instagram.com/examplebuilder/">Instagram</a>
            <a href="https://www.youtube.com/@examplebuilder">YouTube</a>
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


def _forbid_social_calls(monkeypatch, *, overall_recompute: bool = False) -> tuple[list[str], Any]:
    """Replace every way the worker can reach social discovery, a social provider (Apify,
    YouTube, Google Places, connected YouTube), the social LLM polish or the social scoring with a
    recorder, and return (calls, a recording social collector). The worker swallows errors in its
    optional social step, so the record, not an exception, is the proof that nothing ran.

    ``overall_recompute`` leaves the overall-readiness arithmetic alone: a rerun recomputes it
    from the scores an old combined audit already stores, without collecting anything."""
    calls: list[str] = []

    def forbidden(name: str):
        def record(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not run while social audits are off")

        return record

    targets = [
        (tasks, "discover_social_links"),
        (tasks, "get_provider"),
        (tasks, "collect_google_business_facts"),
        (tasks, "latest_google_connection"),
        (tasks, "fetch_own_channel"),
        (tasks, "fetch_channel_analytics"),
        (tasks, "generate_social_commentary"),
        (tasks, "score_social_audit"),
        (tasks, "render_social_pdf"),
        (providers, "fetch_instagram_profile"),
        (providers, "fetch_facebook_page"),
        (providers, "fetch_facebook_posts"),
        (providers, "fetch_youtube_channel"),
        (places_provider, "fetch_place_id"),
        (places_provider, "fetch_place_details"),
    ]
    if not overall_recompute:
        targets.append((tasks, "compose_overall_readiness_score"))
    for module, name in targets:
        monkeypatch.setattr(module, name, forbidden(f"{module.__name__}.{name}"))
    return calls, forbidden("social_collector")


def _add_job(session_factory, **fields: Any) -> str:
    with session_factory() as db:
        job = AuditJob(status="queued", current_stage="Queued", progress_pct=0, **fields)
        db.add(job)
        db.commit()
        db.refresh(job)
        return str(job.id)


def _report_texts(result: Any) -> tuple[str, str]:
    """The rendered PDF's text and the DOCX's text, as the worker stored them."""
    pdf_text = " ".join(page.extract_text() or "" for page in PdfReader(result.pdf_path).pages)
    with ZipFile(result.report_metadata["docx_path"]) as archive:
        docx_xml = archive.read("word/document.xml").decode("utf-8")
    docx_text = re.sub(r"<[^>]+>", " ", docx_xml).replace("&amp;", "&")
    return " ".join(pdf_text.split()), docx_text


def _api_detail(session_factory, job_id: str) -> dict:
    def override_db() -> Generator[Session, None, None]:
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db_session] = override_db
    try:
        response = TestClient(app).get(f"/audits/{job_id}")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    return response.json()


def _assert_website_only(job: AuditJob, settings: Settings) -> None:
    result = job.result
    assert result.seo_score is not None and result.lead_gen_score is not None
    assert result.social_score is None
    assert result.social_facts in (None, {})
    assert "social" not in result.score_breakdown
    assert "overall_readiness" not in result.score_breakdown
    payload = compose_report_payload(job, result, settings=settings)
    assert payload.social_audit is None and payload.overall_readiness is None
    assert payload.combined_complete is False
    # The headline falls back to the website's own Lead Generation Readiness.
    lead_gen = next(card for card in payload.scores if card.id == "lead_gen")
    assert lead_gen.label == "Lead Generation Readiness"
    assert lead_gen.score == result.lead_gen_score
    assert "social audit" not in lead_gen.description
    pdf_text, docx_text = _report_texts(result)
    for text in SOCIAL_REPORT_TEXT:
        assert text not in pdf_text
        assert text not in docx_text
    assert "Website Audit Report" in pdf_text


def test_off_website_audit_never_looks_for_social_links(tmp_path, monkeypatch) -> None:
    # The site links Instagram and YouTube and every provider key is set: with the switch on
    # this audit would be promoted to a combined one. Off, nothing social runs.
    session_factory = _session(tmp_path)
    _patch_settings(
        monkeypatch, session_factory, tmp_path, social_audits_enabled=False, **SOCIAL_KEYS
    )
    calls, collector = _forbid_social_calls(monkeypatch)
    job_id = _add_job(session_factory, url="https://example.com/", audit_type="website")

    tasks.run_collection_audit(
        job_id,
        crawler=_crawler_linking_instagram_and_youtube,
        psi_collector=_fake_psi,
        social_collector=collector,
    )

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.audit_type == "website"
        assert job.social_handles in (None, {})
        _assert_website_only(job, tasks.get_settings())
    detail = _api_detail(session_factory, job_id)
    assert detail["social_score"] is None and detail["overall_score"] is None
    assert detail["social_report"] is None
    assert detail["report"]["social_audit"] is None
    assert detail["report"]["overall_readiness"] is None


def test_off_ignores_the_handles_of_a_combined_job_queued_before_the_switch(
    tmp_path, monkeypatch
) -> None:
    session_factory = _session(tmp_path)
    _patch_settings(
        monkeypatch, session_factory, tmp_path, social_audits_enabled=False, **SOCIAL_KEYS
    )
    calls, collector = _forbid_social_calls(monkeypatch)
    handles = {"instagram": "acme", "youtube": "@acmetube"}
    job_id = _add_job(
        session_factory, url="https://example.com/", audit_type="combined", social_handles=handles
    )

    tasks.run_collection_audit(
        job_id,
        crawler=_crawler_linking_instagram_and_youtube,
        psi_collector=_fake_psi,
        social_collector=collector,
    )

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        # The stored request is left as it was; it is simply not acted on.
        assert (job.audit_type, job.social_handles) == ("combined", handles)
        _assert_website_only(job, tasks.get_settings())

    # Operator path: rerunning the enrichment must not bring social back.
    tasks.rerun_external_enrichment_for_audit(job_id)

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        _assert_website_only(job, tasks.get_settings())


class _WorkerLost(BaseException):
    """Stands in for the worker being killed mid-task: like SIGKILL, it is not an Exception, so
    the task's handlers do not mark the job failed and it stays in progress."""


def test_off_drops_social_data_left_by_an_attempt_lost_before_the_switch(
    tmp_path, monkeypatch
) -> None:
    # Attempt 1 runs before the switch: the site's Instagram link promotes the website audit to
    # a combined one and the social merge is committed. The worker is then lost while rendering
    # (the mid-audit deploy that switches social off), and acks_late redelivers the task.
    session_factory = _session(tmp_path)
    _patch_settings(monkeypatch, session_factory, tmp_path)
    strong = json.loads((FIXTURES / "social_instagram_strong.json").read_text())

    def collector(settings, handles):
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acme", "raw": strong}], now=NOW
        )

    def lost(*_args: Any, **_kwargs: Any) -> Any:
        raise _WorkerLost()

    real_render = tasks.render_audit_pdf
    monkeypatch.setattr(tasks, "render_audit_pdf", lost)
    job_id = _add_job(session_factory, url="https://example.com/", audit_type="website")
    with pytest.raises(_WorkerLost):
        tasks.run_collection_audit(
            job_id,
            crawler=_crawler_linking_instagram_and_youtube,
            psi_collector=_fake_psi,
            social_collector=collector,
        )
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert (job.status, job.audit_type) == ("rendering", "combined")
        assert job.result.social_score is not None and job.result.social_facts

    # Attempt 2, the redelivery, runs with the switch off: the audit completes website-only.
    monkeypatch.setattr(tasks, "render_audit_pdf", real_render)
    _patch_settings(
        monkeypatch, session_factory, tmp_path, social_audits_enabled=False, **SOCIAL_KEYS
    )
    calls, forbidden_collector = _forbid_social_calls(monkeypatch)
    tasks.run_collection_audit(
        job_id,
        crawler=_crawler_linking_instagram_and_youtube,
        psi_collector=_fake_psi,
        social_collector=forbidden_collector,
    )

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        _assert_website_only(job, tasks.get_settings())
    detail = _api_detail(session_factory, job_id)
    assert detail["social_score"] is None and detail["overall_score"] is None
    assert detail["report"]["social_audit"] is None

    # Operator path: a later enrichment rerun must not bring the overall score back.
    tasks.rerun_external_enrichment_for_audit(job_id)

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        _assert_website_only(job, tasks.get_settings())


def test_off_fails_a_social_only_job_queued_before_the_switch(tmp_path, monkeypatch) -> None:
    session_factory = _session(tmp_path)
    _patch_settings(
        monkeypatch, session_factory, tmp_path, social_audits_enabled=False, **SOCIAL_KEYS
    )
    calls, collector = _forbid_social_calls(monkeypatch)
    job_id = _add_job(
        session_factory,
        url="https://www.instagram.com/acme/",
        audit_type="social",
        social_handles={"instagram": "acme"},
    )

    tasks.run_collection_audit(job_id, social_collector=collector)

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "failed"
        assert job.error_message == SOCIAL_AUDITS_DISABLED_MESSAGE
        assert job.completed_at is not None
        assert job.result is None


def test_social_step_is_a_no_op_while_off() -> None:
    # Defence in depth: called directly, the social step returns before touching the job, the
    # session or the collector (None for all three would raise if it did).
    off = Settings(_env_file=None, social_audits_enabled=False, **SOCIAL_KEYS)

    def collector(*_args: Any) -> Any:
        raise AssertionError("no collection while social audits are off")

    assert (
        tasks._augment_with_social(
            None, None, None, off, collector, {"instagram": "acme"}, promote=False
        )
        is None
    )


def test_off_keeps_what_an_audit_completed_before_the_switch_has(tmp_path, monkeypatch) -> None:
    session_factory = _session(tmp_path)
    _patch_settings(monkeypatch, session_factory, tmp_path)
    strong = json.loads((FIXTURES / "social_instagram_strong.json").read_text())

    def collector(settings, handles):
        return extract_social_facts(
            [{"platform": "instagram", "handle": "acme", "raw": strong}], now=NOW
        )

    job_id = _add_job(
        session_factory,
        url="https://example.com/",
        audit_type="combined",
        social_handles={"instagram": "acme"},
    )
    tasks.run_collection_audit(
        job_id, crawler=_fake_crawler, psi_collector=_fake_psi, social_collector=collector
    )
    with session_factory() as db:
        before = db.get(AuditJob, UUID(job_id)).result
        social_score = before.social_score
        overall = before.score_breakdown["overall_readiness"]
        pdf_text, docx_text = _report_texts(before)
    assert social_score is not None and overall["status"] == "complete"
    # Positive control for the report-text checks in _assert_website_only: they do see a social
    # section when a report has one.
    assert "Social Media Audit" in pdf_text and "Social Media Audit" in docx_text

    # The switch goes off; an operator rerun re-renders the old audit without collecting.
    _patch_settings(monkeypatch, session_factory, tmp_path, social_audits_enabled=False)
    calls, _collector = _forbid_social_calls(monkeypatch, overall_recompute=True)
    tasks.rerun_external_enrichment_for_audit(job_id)

    assert calls == []
    with session_factory() as db:
        job = db.get(AuditJob, UUID(job_id))
        assert job.status == "complete"
        assert job.result.social_score == social_score
        assert job.result.score_breakdown["overall_readiness"] == overall
        payload = compose_report_payload(job, job.result, settings=tasks.get_settings())
        assert payload.social_audit is not None
        assert payload.overall_readiness == overall
        pdf_text, docx_text = _report_texts(job.result)
    assert "Social Media Audit" in pdf_text and "Social Media Audit" in docx_text


# ----------------------------------------------------------------------------------- deployment
def test_the_rick_deployment_switches_social_audits_off() -> None:
    # The api and the worker must agree (the API refuses, the worker skips); the UI build drops
    # the form's social fields. The code default stays on, so the production compose file must
    # pin it off, like the edition's other switches.
    services = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]
    for name in ("api", "worker"):
        assert services[name]["environment"]["SOCIAL_AUDITS_ENABLED"] == "false"
    assert services["frontend"]["build"]["args"]["NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED"] == "false"

    dockerfile = (ROOT / "apps/frontend/Dockerfile").read_text()
    assert "ARG NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED" in dockerfile
    assert "NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED=$NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED" in dockerfile

    template = (ROOT / ".env.template").read_text()
    assert re.search(r"^SOCIAL_AUDITS_ENABLED=false$", template, flags=re.M)
    assert re.search(r"^NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED=false$", template, flags=re.M)
