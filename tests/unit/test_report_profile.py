"""Teaser report profile (Rick edition): problems and scores on every surface, never a fix.

The leak checks start from the SOURCES of fix text (every rubric ``remediation``, the content
plan's action titles, the hard-coded technical-SEO fixes, the executive-summary advice), not
from field names, so a fix that reaches a surface through a new or renamed field still fails.
The fixture is the real pipeline (extract -> score -> content plan) over the weak-site HTML
fixture, where most checks fail, plus a technical crawl, an axe pass and a scored social
bundle, so every block that can carry a fix is populated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
from zipfile import ZipFile

import pytest
import yaml
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.deps import get_db_session
from apps.api.main import app
from apps.shared.audit_states import AuditStatus
from apps.shared.config import Settings
from apps.shared.models import AuditJob, AuditResult, Base
from apps.worker.stages import report_payload as report_payload_module
from apps.worker.stages.content_plan import (
    _ACTION_TITLES,
    EXECUTIVE_SUMMARY_CLOSER,
    HOMEPAGE_LOCATION_LABEL,
    LOCATION_LABEL,
    build_content_plan,
)
from apps.worker.stages.docx_renderer import render_report_docx
from apps.worker.stages.extractor_seo import extract_seo_facts
from apps.worker.stages.extractor_uxui import extract_uxui_facts
from apps.worker.stages.pdf_renderer import render_report_pdf, render_social_pdf
from apps.worker.stages.report_payload import (
    GENERIC_TECHNICAL_ISSUE_GUIDANCE,
    TECHNICAL_ISSUE_GUIDANCE,
    compose_report_payload,
)
from apps.worker.stages.report_profile import FALLBACK_SUMMARY_ADVICE, apply_report_profile
from apps.worker.stages.scoring import score_audit, score_social_audit
from apps.worker.stages.social import report as social_report_module
from apps.worker.stages.social.extractor import extract_social_facts
from apps.worker.stages.social.report import (
    compose_social_report_data,
    compose_social_report_payload,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
BOOKING_URL = "https://example.com/book-rick"
AXE_FIX = "Fix any of the following: Element does not have an alt attribute"
AXE_HELP_URL = "https://dequeuniversity.com/rules/axe/4.10/image-alt"
LLM_NARRATIVE = "Post twice a week and pin a booking link to the top of the profile."
LLM_SUMMARY = "Posting more consistently and adding a booking link will lift engagement."
# Keys whose value IS a fix or a fix's schedule ("tier" is the roadmap horizon). In a teaser
# each must be empty wherever it appears.
FIX_KEYS = {
    "action_items",
    "recommendations",
    "roadmap",
    "recommended_fix",
    "remediation",
    "narrative",
    "failure_summary",
    "help_url",
    "tier",
}


def _fix_corpus() -> set[str]:
    corpus = {
        EXECUTIVE_SUMMARY_CLOSER,
        FALLBACK_SUMMARY_ADVICE,
        LOCATION_LABEL,
        HOMEPAGE_LOCATION_LABEL,
        AXE_FIX,
        AXE_HELP_URL,
        LLM_NARRATIVE,
        LLM_SUMMARY,
        *_ACTION_TITLES.values(),
    }
    for guidance in [*TECHNICAL_ISSUE_GUIDANCE.values(), GENERIC_TECHNICAL_ISSUE_GUIDANCE]:
        corpus.add(guidance["recommended_fix"])
    for name in ("seo.yaml", "uxui.yaml", "social.yaml"):
        for rule in yaml.safe_load((ROOT / "rubrics" / name).read_text())["rules"]:
            if rule.get("remediation"):
                corpus.add(rule["remediation"])
    return {" ".join(text.split()) for text in corpus}


FIX_CORPUS = _fix_corpus()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _leaks(text: str) -> list[str]:
    # Case-insensitive: the PDF stylesheet uppercases some labels and headings.
    haystack = " ".join(text.split()).lower()
    return sorted(fix for fix in FIX_CORPUS if fix.lower() in haystack)


def _json_leaks(value: Any) -> list[str]:
    return _leaks("\n".join(_strings(value)))


def _filled_fix_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FIX_KEYS and item:
                found.append(f"{path}.{key}")
            found.extend(_filled_fix_keys(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_filled_fix_keys(item, f"{path}[{index}]"))
    return found


def _settings(tmp_path: Path | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "booking_url": BOOKING_URL}
    if tmp_path is not None:
        values["local_report_storage_dir"] = tmp_path
    values.update(overrides)
    return Settings(**values)


def _teaser(tmp_path: Path | None = None) -> Settings:
    return _settings(tmp_path, report_profile="teaser")


def _external_seo_facts() -> dict:
    # One issue per hard-coded guidance entry (plus an unknown id for the generic fallback), so
    # every "recommended fix" sentence is exercised.
    ids = [*TECHNICAL_ISSUE_GUIDANCE, "unmapped_issue"]
    return {
        "status": "complete",
        "technical_crawl": {
            "status": "complete",
            "source": "site_health_sweep",
            "summary": {"urls_crawled": 12},
            "issues": [
                {
                    "id": issue_id,
                    "severity": "high",
                    "title": f"Issue {issue_id}",
                    "count": 1,
                    "examples": [f"https://weak.example/{index}"],
                }
                for index, issue_id in enumerate(ids)
            ],
        },
    }


def _accessibility_facts() -> dict:
    return {
        "status": "complete",
        "axe_version": "4.10.2",
        "pages_scanned": 1,
        "impact_counts": {"critical": 1},
        "issues": [
            {
                "rule_id": "image-alt",
                "impact": "critical",
                "wcag_criteria": ["wcag111"],
                "help": "Images must have alternate text",
                "help_url": AXE_HELP_URL,
                "instances": 3,
                "example_selectors": ["img.hero"],
                "example_pages": ["https://weak.example/"],
                "failure_summary": AXE_FIX,
            }
        ],
    }


def _social() -> tuple[dict, dict]:
    raw = json.loads((FIXTURES / "social_instagram_weak.json").read_text())
    facts = extract_social_facts(
        [{"platform": "instagram", "handle": "weak", "raw": raw}],
        now=datetime(2026, 6, 23, tzinfo=UTC),
    )
    return facts, score_social_audit(facts, Settings(_env_file=None))


def _job(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "url": "https://weak.example/",
        "niche": "builder",
        "target_audience": "homeowners",
        "audit_type": "website",
        "social_handles": None,
        "brand_overrides": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _website_result(*, combined: bool = False) -> SimpleNamespace:
    settings = Settings(_env_file=None)
    html = (FIXTURES / "weak_site.html").read_text(encoding="utf-8")
    pages = [
        {
            "url": "https://weak.example/",
            "final_url": "https://weak.example/",
            "status_code": 200,
            "html": html,
        }
    ]
    seo_facts = extract_seo_facts(pages)
    uxui_facts = extract_uxui_facts(pages)
    psi_facts = {"status": "skipped", "summary": {}}
    external = _external_seo_facts()
    breakdown = score_audit(seo_facts, uxui_facts, psi_facts, settings, external)
    plan = build_content_plan(
        audit_context={"url": "https://weak.example/"},
        seo_facts=seo_facts,
        uxui_facts=uxui_facts,
        psi_facts=psi_facts,
        external_seo_facts=external,
        score_breakdown=breakdown,
        settings=settings,
    )
    social_facts: dict = {}
    social_score = None
    if combined:
        social_facts, social_breakdown = _social()
        social_score = social_breakdown["score"]
        breakdown["social"] = social_breakdown
    scores = breakdown["scores"]
    return SimpleNamespace(
        seo_score=scores["seo"],
        uxui_score=scores["uxui"],
        lead_gen_score=scores["lead_gen"],
        social_score=social_score,
        crawled_pages={
            "status": "complete",
            "final_url": "https://weak.example/",
            "summary": {"successful_pages": 1, "failed_pages": 0, "skipped_pages": 0},
        },
        seo_facts=seo_facts,
        uxui_facts=uxui_facts,
        psi_facts=psi_facts,
        external_seo_facts=external,
        social_facts=social_facts,
        accessibility_facts=_accessibility_facts(),
        score_breakdown=breakdown,
        commentary={"provider": "deterministic", "content": plan.model_dump(mode="json")},
        validation_log={"status": "complete"},
        report_metadata={},
        pdf_path=None,
        rubric_version=breakdown.get("rubric_version", "test"),
        llm_model="deterministic",
    )


def _social_result() -> SimpleNamespace:
    """A standalone social result whose stored commentary is an LLM polish that states fixes."""
    facts, breakdown = _social()
    baseline = compose_social_report_data(
        _job(audit_type="social"),
        SimpleNamespace(
            social_facts=facts,
            score_breakdown=breakdown,
            social_score=breakdown["score"],
            commentary=None,
        ),
    )
    commentary = {
        "status": "llm",
        "provider": "openai",
        "model": "gpt-4o",
        "content": {
            "executive_summary": LLM_SUMMARY,
            "findings": [
                {"id": finding["id"], "title": finding["label"], "narrative": LLM_NARRATIVE}
                for finding in baseline["findings"]
            ],
        },
    }
    return SimpleNamespace(
        social_facts=facts,
        score_breakdown=breakdown,
        social_score=breakdown["score"],
        commentary=commentary,
        report_metadata={},
        pdf_path=None,
    )


# --- the JSON payload (what /audits/{id}, /shared/{token}, the UI and both renderers read) ---


@pytest.mark.parametrize("combined", [False, True])
def test_full_payload_carries_the_fixes_the_teaser_must_hide(combined: bool) -> None:
    # Guards the leak tests below against a vacuous fixture: the FULL payload of the same
    # result really does contain rubric remediation, action titles and technical fixes.
    payload = compose_report_payload(
        _job(), _website_result(combined=combined), settings=_settings()
    ).model_dump(mode="json")
    leaks = _json_leaks(payload)
    assert any(fix in _ACTION_TITLES.values() for fix in leaks)
    assert any(g["recommended_fix"] in leaks for g in TECHNICAL_ISSUE_GUIDANCE.values())
    assert EXECUTIVE_SUMMARY_CLOSER in leaks
    assert LOCATION_LABEL in leaks and HOMEPAGE_LOCATION_LABEL in leaks
    assert payload["roadmap"] and payload["report_profile"] == "full" and payload["cta"] is None
    if combined:
        assert payload["social_audit"]["findings"][0]["remediation"]


@pytest.mark.parametrize("combined", [False, True])
def test_teaser_payload_contains_no_fix(combined: bool) -> None:
    payload = compose_report_payload(
        _job(), _website_result(combined=combined), settings=_teaser()
    ).model_dump(mode="json")

    assert _json_leaks(payload) == []
    assert _filled_fix_keys(payload) == []
    assert payload["report_profile"] == "teaser"
    assert payload["cta"]["url"] == BOOKING_URL
    assert payload["cta"]["label"] == "Book a meeting with Rick"


def test_teaser_keeps_every_problem_and_score() -> None:
    result = _website_result(combined=True)
    full = compose_report_payload(_job(), result, settings=_settings())
    teaser = compose_report_payload(_job(), result, settings=_teaser())

    assert teaser.scores == full.scores
    assert teaser.overall_readiness == full.overall_readiness
    for full_section, teaser_section in zip(full.sections, teaser.sections, strict=True):
        assert teaser_section.score == full_section.score
        assert [(f.severity, f.title, f.meaning, f.why) for f in teaser_section.findings] == [
            (f.severity, f.title, f.meaning, f.why) for f in full_section.findings
        ]
        assert teaser_section.opportunities == full_section.opportunities
    assert [i.title for i in teaser.technical_seo_section.issues] == [
        i.title for i in full.technical_seo_section.issues
    ]
    assert teaser.appendix == full.appendix
    full_social, teaser_social = full.social_audit, teaser.social_audit
    assert [(f["label"], f["metric"], f["impact"]) for f in teaser_social["findings"]] == [
        (f["label"], f["metric"], f["impact"]) for f in full_social["findings"]
    ]
    # The summary loses only its how-to-proceed advice.
    assert teaser.executive_summary == " ".join(
        full.executive_summary.replace(EXECUTIVE_SUMMARY_CLOSER, "").split()
    )


def test_full_profile_is_a_no_op() -> None:
    payload = compose_report_payload(_job(), _website_result(), settings=_settings())
    assert apply_report_profile(payload, _settings()) is payload


def test_profile_comes_from_the_configured_settings_by_default(monkeypatch) -> None:
    # The API calls the composer without settings; the process's settings decide.
    monkeypatch.setattr(report_payload_module, "get_settings", _teaser)
    payload = compose_report_payload(_job(), _website_result()).model_dump(mode="json")
    assert payload["report_profile"] == "teaser"
    assert _json_leaks(payload) == []


def test_teaser_hides_an_ai_visibility_block_that_could_not_collect() -> None:
    # The only content of an uncollected block is an operator note ("reconnect Semrush").
    result = _website_result()
    result.score_breakdown["ai_visibility"] = {
        "status": "failed",
        "reason": "no_session",
        "provider": "semrush",
        "domain": "weak.example",
    }
    full = compose_report_payload(_job(), result, settings=_settings())
    teaser = compose_report_payload(_job(), result, settings=_teaser())
    assert full.ai_visibility and full.ai_visibility["unavailable"]
    assert teaser.ai_visibility is None


def test_teaser_keeps_collected_ai_visibility_data() -> None:
    result = _website_result()
    result.score_breakdown["ai_visibility"] = {
        "status": "complete",
        "provider": "semrush",
        "domain": "weak.example",
        "visibility_score": 19,
        "visibility_band": "Low",
        "mentions": 28,
        "per_platform": [{"platform": "AI Overview", "mentions": 22, "share_pct": 78.6}],
    }
    teaser = compose_report_payload(_job(), result, settings=_teaser())
    assert teaser.ai_visibility and teaser.ai_visibility["visibility_score"] == 19


# --- standalone social report ---


def test_teaser_social_report_drops_fixes_and_llm_prose() -> None:
    job = _job(audit_type="social", social_handles={"instagram": "weak"})
    result = _social_result()
    full = compose_social_report_payload(job, result, settings=_settings())
    teaser = compose_social_report_payload(job, result, settings=_teaser())

    assert full["findings"] and full["findings"][0]["narrative"] == LLM_NARRATIVE
    assert full["report_profile"] == "full" and full["cta"] is None
    assert _json_leaks(teaser) == []
    assert _filled_fix_keys(teaser) == []
    assert teaser["executive_summary"].startswith(
        f"This social presence scored {result.social_score}/100"
    )
    assert teaser["commentary_provider"] == "deterministic"
    assert teaser["cta"]["url"] == BOOKING_URL
    assert [f["label"] for f in teaser["findings"]] == [f["label"] for f in full["findings"]]


def test_worker_commentary_input_keeps_the_fixes_in_teaser_mode(monkeypatch) -> None:
    # Stored data must not depend on how reports render: the worker's social commentary step
    # reads compose_social_report_data, which ignores the profile.
    monkeypatch.setattr(social_report_module, "get_settings", _teaser)
    job = _job(audit_type="social", social_handles={"instagram": "weak"})
    data = compose_social_report_data(job, _social_result())
    assert any(finding["remediation"] for finding in data["findings"])


# --- rendered files ---


def _pdf_text(path: Path) -> str:
    # Lower-cased: the stylesheet uppercases labels such as "Do this" in the PDF.
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages).lower()


def _docx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", " ", document).lower()


def test_teaser_pdf_contains_no_fix(tmp_path) -> None:
    result = _website_result(combined=True)
    full_pdf, teaser_pdf = tmp_path / "full.pdf", tmp_path / "teaser.pdf"
    render_report_pdf(
        compose_report_payload(_job(), result, settings=_settings()),
        settings=_settings(tmp_path),
        output_path=full_pdf,
    )
    render_report_pdf(
        compose_report_payload(_job(), result, settings=_teaser()),
        settings=_teaser(tmp_path),
        output_path=teaser_pdf,
    )
    full_text, teaser_text = _pdf_text(full_pdf), _pdf_text(teaser_pdf)

    assert _leaks(full_text), "fixture must put fix text in the full PDF"
    assert "do this" in full_text
    assert _leaks(teaser_text) == []
    for label in ("do this", "recommended fix", "how to fix", "action roadmap", "quick wins"):
        assert label in full_text and label not in teaser_text
    assert "book a meeting with rick" in teaser_text
    assert BOOKING_URL in teaser_text


def test_teaser_docx_contains_no_fix(tmp_path) -> None:
    result = _website_result(combined=True)
    full_docx, teaser_docx = tmp_path / "full.docx", tmp_path / "teaser.docx"
    render_report_docx(
        compose_report_payload(_job(), result, settings=_settings()), output_path=full_docx
    )
    render_report_docx(
        compose_report_payload(_job(), result, settings=_teaser()), output_path=teaser_docx
    )
    full_text, teaser_text = _docx_text(full_docx), _docx_text(teaser_docx)

    assert _leaks(full_text), "fixture must put fix text in the full DOCX"
    assert _leaks(teaser_text) == []
    for label in ("do this", "lead generation roadmap"):
        assert label in full_text and label not in teaser_text
    assert "book a meeting with rick" in teaser_text and BOOKING_URL in teaser_text


def test_teaser_social_pdf_contains_no_fix(tmp_path) -> None:
    job = _job(audit_type="social", social_handles={"instagram": "weak"})
    render_social_pdf(job, _social_result(), _teaser(tmp_path))
    text = _pdf_text(tmp_path / f"{job.id}.pdf")

    assert _leaks(text) == []
    assert "what we found" in text and "book a meeting with rick" in text


# --- the HTTP surfaces a visitor can reach ---


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.mark.parametrize("audit_type", ["combined", "social"])
def test_api_detail_and_share_link_json_contain_no_fix(monkeypatch, audit_type: str) -> None:
    monkeypatch.setattr(report_payload_module, "get_settings", _teaser)
    monkeypatch.setattr(social_report_module, "get_settings", _teaser)
    stored = _website_result(combined=True) if audit_type == "combined" else _social_result()
    factory = _session_factory()
    token = "teaser-token"
    with factory() as db:
        job = AuditJob(
            url="https://weak.example/",
            audit_type=audit_type,
            social_handles={"instagram": "weak"},
            status=AuditStatus.COMPLETE.value,
            current_stage="Complete",
            progress_pct=100,
            share_token=token,
            share_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(job)
        db.flush()
        db.add(
            AuditResult(
                job_id=job.id,
                seo_score=getattr(stored, "seo_score", None),
                uxui_score=getattr(stored, "uxui_score", None),
                lead_gen_score=getattr(stored, "lead_gen_score", None),
                social_score=stored.social_score,
                crawled_pages=getattr(stored, "crawled_pages", {}),
                seo_facts=getattr(stored, "seo_facts", {}),
                uxui_facts=getattr(stored, "uxui_facts", {}),
                psi_facts=getattr(stored, "psi_facts", {}),
                external_seo_facts=getattr(stored, "external_seo_facts", {}),
                social_facts=stored.social_facts,
                accessibility_facts=getattr(stored, "accessibility_facts", None),
                score_breakdown=stored.score_breakdown,
                commentary=stored.commentary,
                validation_log=getattr(stored, "validation_log", {}),
                report_metadata={},
                pdf_path=None,
                rubric_version="test",
                llm_model="deterministic",
            )
        )
        db.commit()
        job_id = job.id

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        detail = client.get(f"/audits/{job_id}")
        shared = client.get(f"/shared/{token}")
    finally:
        app.dependency_overrides.clear()

    assert detail.status_code == 200 and shared.status_code == 200
    for body in (detail.json(), shared.json()):
        report = body["report"] if audit_type == "combined" else body["social_report"]
        assert report["report_profile"] == "teaser"
        assert _json_leaks(body) == []
        assert _filled_fix_keys(body) == []


def test_booking_url_must_be_a_link() -> None:
    with pytest.raises(ValueError, match="booking_url"):
        Settings(_env_file=None, booking_url="javascript:alert(1)")
    assert Settings(_env_file=None, booking_url="mailto:rick@example.com").booking_url
