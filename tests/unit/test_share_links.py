import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.deps import get_db_session
from apps.api.main import app
from apps.api.routes import audits as audit_routes
from apps.shared.audit_states import AuditStatus
from apps.shared.models import AuditJob, AuditResult, Base


def _make_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _seed_complete_job(
    session_factory,
    pdf_path: Path,
    *,
    share_token: str | None = None,
    share_expires_at: datetime | None = None,
) -> uuid.UUID:
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    with session_factory() as db:
        job = AuditJob(
            url="https://example.com/",
            status=AuditStatus.COMPLETE.value,
            current_stage="Audit report complete",
            progress_pct=100,
            share_token=share_token,
            share_expires_at=share_expires_at,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.add(
            AuditResult(
                job_id=job.id,
                seo_score=82,
                uxui_score=74,
                lead_gen_score=78,
                crawled_pages={"final_url": "https://example.com/", "summary": {}},
                seo_facts={},
                uxui_facts={},
                psi_facts={"status": "ok", "summary": {}},
                score_breakdown={},
                commentary={"content": {"executive_summary": "Strong foundation."}},
                validation_log={"status": "passed"},
                report_metadata={"renderer": "weasyprint"},
                pdf_path=str(pdf_path),
                rubric_version="phase1-test",
                llm_model="deterministic",
            )
        )
        db.commit()
        return job.id


def _client(session_factory, monkeypatch) -> TestClient:
    def override_db() -> Generator[Session, None, None]:
        with session_factory() as db:
            yield db

    monkeypatch.setattr(
        audit_routes, "get_settings", lambda: SimpleNamespace(share_link_ttl_days=7)
    )
    app.dependency_overrides[get_db_session] = override_db
    return TestClient(app)


def test_share_generate_then_public_access(tmp_path, monkeypatch) -> None:
    factory = _make_session_factory()
    job_id = _seed_complete_job(factory, tmp_path / "a.pdf")
    client = _client(factory, monkeypatch)
    try:
        gen = client.post(f"/audits/{job_id}/share")
        assert gen.status_code == 200
        body = gen.json()
        token = body["share_token"]
        assert token
        assert body["report_path"] == f"/shared/{token}/report"

        meta = client.get(f"/shared/{token}")
        assert meta.status_code == 200
        assert meta.json()["report"]["executive_summary"] == "Strong foundation."

        pdf = client.get(f"/shared/{token}/report")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
    finally:
        app.dependency_overrides.clear()


def test_share_revoke_blocks_public_access(tmp_path, monkeypatch) -> None:
    factory = _make_session_factory()
    job_id = _seed_complete_job(factory, tmp_path / "a.pdf")
    client = _client(factory, monkeypatch)
    try:
        token = client.post(f"/audits/{job_id}/share").json()["share_token"]
        assert client.get(f"/shared/{token}").status_code == 200

        revoke = client.delete(f"/audits/{job_id}/share")
        assert revoke.status_code == 200
        assert revoke.json()["shared"] is False

        assert client.get(f"/shared/{token}").status_code == 404
        assert client.get(f"/shared/{token}/report").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_share_expired_token_is_gone(tmp_path, monkeypatch) -> None:
    factory = _make_session_factory()
    past = datetime.now(UTC) - timedelta(days=1)
    _seed_complete_job(
        factory, tmp_path / "a.pdf", share_token="expired-token-xyz", share_expires_at=past
    )
    client = _client(factory, monkeypatch)
    try:
        assert client.get("/shared/expired-token-xyz").status_code == 410
        assert client.get("/shared/expired-token-xyz/report").status_code == 410
    finally:
        app.dependency_overrides.clear()


def test_share_unknown_token_is_not_found(tmp_path, monkeypatch) -> None:
    factory = _make_session_factory()
    _seed_complete_job(factory, tmp_path / "a.pdf")
    client = _client(factory, monkeypatch)
    try:
        assert client.get("/shared/does-not-exist").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_share_requires_a_report(monkeypatch) -> None:
    factory = _make_session_factory()
    with factory() as db:
        job = AuditJob(
            url="https://example.com/", status=AuditStatus.CRAWLING.value, progress_pct=15
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    client = _client(factory, monkeypatch)
    try:
        assert client.post(f"/audits/{job_id}/share").status_code == 409
    finally:
        app.dependency_overrides.clear()


def _seed_complete_social_job(session_factory, pdf_path: Path) -> uuid.UUID:
    pdf_path.write_bytes(b"%PDF-1.4\n% social\n")
    with session_factory() as db:
        job = AuditJob(
            url="https://www.instagram.com/acmebuilders/",
            audit_type="social",
            social_handles={"instagram": "acmebuilders"},
            status=AuditStatus.COMPLETE.value,
            current_stage="Social audit complete",
            progress_pct=100,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.add(
            AuditResult(
                job_id=job.id,
                social_score=88,
                social_facts={
                    "status": "complete",
                    "summary": {"platforms_audited": 1},
                    "platforms": [],
                },
                crawled_pages={},
                seo_facts={},
                uxui_facts={},
                psi_facts={},
                score_breakdown={"category": {"category": "social", "rules": []}},
                commentary={},
                validation_log={},
                report_metadata={"report_kind": "social"},
                pdf_path=str(pdf_path),
                rubric_version="phase2-social-v1",
                llm_model="deterministic",
            )
        )
        db.commit()
        return job.id


def test_share_social_audit_serves_social_report(tmp_path, monkeypatch) -> None:
    # A shared social audit must use the social composer, not the website one (which would
    # run on a result whose seo/uxui scores are NULL and seo_facts empty).
    factory = _make_session_factory()
    job_id = _seed_complete_social_job(factory, tmp_path / "social.pdf")
    client = _client(factory, monkeypatch)
    try:
        token = client.post(f"/audits/{job_id}/share").json()["share_token"]

        meta = client.get(f"/shared/{token}")
        assert meta.status_code == 200
        body = meta.json()
        assert body["audit_type"] == "social"
        assert body["report"] is None
        assert body["social_report"]["score"] == 88
        assert body["social_report"]["handles"] == {"instagram": "acmebuilders"}

        pdf = client.get(f"/shared/{token}/report")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
    finally:
        app.dependency_overrides.clear()


def test_create_audit_persists_brand_overrides(monkeypatch) -> None:
    factory = _make_session_factory()

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    monkeypatch.setattr(
        audit_routes, "get_settings", lambda: SimpleNamespace(audit_enqueue_enabled=False)
    )
    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        resp = client.post(
            "/audits",
            json={
                "url": "https://acme.com",
                "brand_overrides": {"name": "Acme", "primary_color": "#112233"},
            },
        )
        assert resp.status_code == 201
        job_id = uuid.UUID(resp.json()["job_id"])
        with factory() as db:
            job = db.get(AuditJob, job_id)
            assert job.brand_overrides == {"name": "Acme", "primary_color": "#112233"}
    finally:
        app.dependency_overrides.clear()
