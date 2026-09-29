from collections.abc import Generator
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.deps import get_db_session
from apps.api.main import app, create_app
from apps.api.routes import audits as audit_routes
from apps.api.routes import google as google_routes
from apps.shared.audit_states import AuditStatus
from apps.shared.config import Settings
from apps.shared.models import AuditJob, AuditResult, Base


def test_swagger_ui_is_available() -> None:
    client = TestClient(app)

    root_response = client.get("/", follow_redirects=False)
    assert root_response.status_code in {307, 308}
    assert root_response.headers["location"] == "/docs"

    docs_response = client.get("/docs")
    assert docs_response.status_code == 200
    assert "Swagger UI" in docs_response.text

    openapi_response = client.get("/openapi.json")
    assert openapi_response.status_code == 200
    assert openapi_response.json()["info"]["title"] == "BLC Website Audit Automation"


def test_docs_and_schema_are_not_served_in_production() -> None:
    # Caddy forwards /api/* without auth, so production must not publish the API map.
    client = TestClient(create_app(Settings(_env_file=None, app_env="production")))

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/", follow_redirects=False).status_code == 404
    assert client.get("/health").status_code == 200


def test_google_connect_url_is_returned_after_app_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        google_routes,
        "get_settings",
        lambda: SimpleNamespace(
            google_oauth_client_id="client-123",
            google_oauth_client_secret=SecretStr("secret"),
            google_oauth_redirect_uri="http://localhost:8000/google/search-console/callback",
        ),
    )
    client = TestClient(app)

    response = client.get("/google/search-console/connect-url")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["reason"] is None
    assert payload["connect_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=client-123" in payload["connect_url"]
    assert "webmasters.readonly" in payload["connect_url"]


def test_create_and_read_audit_lifecycle(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_db() -> Generator[Session, None, None]:
        with TestingSession() as db:
            yield db

    monkeypatch.setattr(
        audit_routes,
        "get_settings",
        lambda: SimpleNamespace(audit_enqueue_enabled=False, public_audits_enabled=False),
    )
    app.dependency_overrides[get_db_session] = override_db

    try:
        client = TestClient(app)
        create_response = client.post(
            "/audits",
            json={
                "url": "https://example.com",
                "niche": "builder",
                "target_audience": "homeowners",
            },
        )

        assert create_response.status_code == 201
        payload = create_response.json()
        assert payload["status"] == "queued"

        status_response = client.get(payload["status_url"])
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["url"] == "https://example.com/"
        assert status_payload["progress_pct"] == 0
        assert status_payload["report_available"] is False

        list_response = client.get("/audits")
        assert list_response.status_code == 200
        assert len(list_response.json()["audits"]) == 1
    finally:
        app.dependency_overrides.clear()


def test_audit_detail_returns_report_payload(tmp_path) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db() -> Generator[Session, None, None]:
        with TestingSession() as db:
            yield db

    pdf_path = tmp_path / "audit.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            niche="custom home builder",
            target_audience="homeowners",
            status=AuditStatus.COMPLETE.value,
            current_stage="Audit report complete",
            progress_pct=100,
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
                llm_model="gpt-4o",
            )
        )
        db.commit()
        job_id = job.id

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        detail_response = client.get(f"/audits/{job_id}")
        missing_response = client.get("/audits/00000000-0000-0000-0000-000000000000")
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["url"] == "https://example.com/"
    assert detail["niche"] == "custom home builder"
    assert detail["report_available"] is True
    report = detail["report"]
    assert report is not None
    scores = {card["id"]: card["score"] for card in report["scores"]}
    assert scores == {"lead_gen": 78, "seo": 82, "uxui": 74}
    assert report["executive_summary"] == "Strong foundation."
    assert {section["id"] for section in report["sections"]} == {"seo", "uxui", "lead_generation"}

    assert missing_response.status_code == 404


def test_audit_detail_without_result_has_null_report() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db() -> Generator[Session, None, None]:
        with TestingSession() as db:
            yield db

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            status=AuditStatus.CRAWLING.value,
            current_stage="Rendering website pages",
            progress_pct=15,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        detail_response = client.get(f"/audits/{job_id}")
    finally:
        app.dependency_overrides.clear()

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["status"] == "crawling"
    assert detail["current_stage"] == "Rendering website pages"
    assert detail["progress_pct"] == 15
    assert detail["report"] is None
    assert detail["report_available"] is False


def test_report_endpoint_streams_generated_pdf(tmp_path) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db() -> Generator[Session, None, None]:
        with TestingSession() as db:
            yield db

    pdf_path = tmp_path / "audit.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            status=AuditStatus.COMPLETE.value,
            current_stage="Audit report complete",
            progress_pct=100,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.add(
            AuditResult(
                job_id=job.id,
                seo_score=80,
                uxui_score=70,
                lead_gen_score=75,
                crawled_pages={},
                seo_facts={},
                uxui_facts={},
                psi_facts={},
                score_breakdown={},
                commentary={},
                validation_log={},
                report_metadata={"renderer": "weasyprint"},
                pdf_path=str(pdf_path),
                rubric_version="phase1-test",
                llm_model="gpt-4o",
            )
        )
        db.commit()
        job_id = job.id

    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        status_response = client.get(f"/audits/{job_id}/status")
        report_response = client.get(f"/audits/{job_id}/report")

        assert status_response.status_code == 200
        assert status_response.json()["report_available"] is True
        assert report_response.status_code == 200
        assert report_response.headers["content-type"] == "application/pdf"
        assert report_response.content.startswith(b"%PDF-1.4")
    finally:
        app.dependency_overrides.clear()


def test_docx_endpoint_generates_editable_report(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db() -> Generator[Session, None, None]:
        with TestingSession() as db:
            yield db

    with TestingSession() as db:
        job = AuditJob(
            url="https://example.com/",
            status=AuditStatus.COMPLETE.value,
            current_stage="Audit report complete",
            progress_pct=100,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.add(
            AuditResult(
                job_id=job.id,
                seo_score=80,
                uxui_score=70,
                lead_gen_score=75,
                crawled_pages={
                    "status": "complete",
                    "final_url": "https://example.com/",
                    "summary": {"successful_pages": 1, "failed_pages": 0, "skipped_pages": 0},
                },
                seo_facts={},
                uxui_facts={},
                psi_facts={"status": "skipped", "summary": {}},
                score_breakdown={"scores": {"seo": 80, "uxui": 70, "lead_gen": 75}},
                commentary={"content": {"executive_summary": "Editable report ready."}},
                validation_log={"status": "complete"},
                report_metadata={},
                pdf_path=None,
                rubric_version="phase1-test",
                llm_model="deterministic",
            )
        )
        db.commit()
        job_id = job.id

    monkeypatch.setattr(
        audit_routes,
        "get_settings",
        lambda: SimpleNamespace(
            local_report_storage_dir=tmp_path, report_profile="full", search_console_enabled=True
        ),
    )
    app.dependency_overrides[get_db_session] = override_db
    try:
        client = TestClient(app)
        response = client.get(f"/audits/{job_id}/docx")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    with ZipFile(BytesIO(response.content)) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Website Audit Report" in document
    assert "Editable report ready." in document


def test_social_primary_url_keeps_full_url_handles_verbatim() -> None:
    # The new-audit form accepts profile links as handles; a URL-shaped handle must be stored
    # verbatim as the job's url, not nested under the platform host into a doubled-domain URL.
    assert (
        audit_routes._social_primary_url({"instagram": "https://www.instagram.com/acme/"})
        == "https://www.instagram.com/acme/"
    )
    assert (
        audit_routes._social_primary_url({"tiktok": "https://www.tiktok.com/@acme"})
        == "https://www.tiktok.com/@acme"
    )
    # Bare handles keep the canonical platform-host form they always had.
    assert (
        audit_routes._social_primary_url({"instagram": "@acme"})
        == "https://www.instagram.com/acme/"
    )
    assert audit_routes._social_primary_url({"youtube": "acme"}) == "https://www.youtube.com/@acme"


def test_rerun_ai_visibility_refuses_when_disabled(monkeypatch) -> None:
    # The kill-switch contract: with AI_VISIBILITY_ENABLED=false the endpoint 409s BEFORE touching
    # the DB or enqueuing anything, so no code path (button included) can reach Semrush while the
    # feature is off. The worker's collector checks the same flag, but the API refusing up front
    # means the operator sees WHY instead of a task that silently stores nothing.
    monkeypatch.setattr(
        audit_routes, "get_settings", lambda: SimpleNamespace(ai_visibility_enabled=False)
    )
    client = TestClient(app)

    response = client.post("/audits/11111111-1111-1111-1111-111111111111/rerun-ai-visibility")

    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]
