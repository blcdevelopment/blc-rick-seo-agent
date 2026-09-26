"""Public audits (Rick edition): visitors create and read audits without signing in, while the
operator endpoints stay gated, and on a production public deployment without Clerk they are
closed rather than open."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api import auth
from apps.api.deps import get_db_session
from apps.api.main import app
from apps.api.routes import audits as audit_routes
from apps.shared.config import Settings
from apps.shared.models import AuditJob, Base

CLERK = "https://clerk.example.test"


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
        return TestClient(app), factory

    app.dependency_overrides[get_db_session] = override_db
    yield build
    app.dependency_overrides.clear()


def _create(client: TestClient, **extra: Any):
    return client.post("/audits", json={"url": "https://prospect.example", **extra})


def test_visitor_endpoints_open_while_operator_endpoints_keep_clerk(client_for) -> None:
    client, _ = client_for(public_audits_enabled=True, clerk_issuer=CLERK)

    created = _create(client)
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    assert client.get(f"/audits/{job_id}").status_code == 200
    assert client.get(f"/audits/{job_id}/status").status_code == 200
    # No report yet: a 404, i.e. auth passed and the handler ran.
    assert client.get(f"/audits/{job_id}/report").status_code == 404

    assert client.get("/audits").status_code == 401  # history lists everyone's audits
    assert client.post(f"/audits/{job_id}/share").status_code == 401
    assert client.post(f"/audits/{job_id}/rerun-enrichment").status_code == 401
    assert client.get("/metrics").status_code == 401
    assert client.get("/google/search-console/connect-url").status_code == 401


def test_without_public_mode_every_audit_endpoint_needs_clerk(client_for) -> None:
    client, _ = client_for(clerk_issuer=CLERK)
    assert _create(client).status_code == 401
    assert client.get("/audits/00000000-0000-0000-0000-000000000000").status_code == 401


def test_public_production_without_clerk_closes_operator_endpoints(client_for) -> None:
    client, _ = client_for(public_audits_enabled=True, app_env="production")

    created = _create(client)
    assert created.status_code == 201
    assert client.get(f"/audits/{created.json()['job_id']}").status_code == 200

    assert client.get("/audits").status_code == 403
    assert client.get("/metrics").status_code == 403
    assert client.get("/google/search-console/connect-url").status_code == 403


def test_local_public_mode_keeps_operator_endpoints_open_without_clerk(client_for) -> None:
    # Same as the parent's local dev: no CLERK_ISSUER means an open API outside production.
    client, _ = client_for(public_audits_enabled=True)
    assert client.get("/audits").status_code == 200


def test_public_visitors_cannot_white_label_a_report(client_for) -> None:
    client, factory = client_for(public_audits_enabled=True)
    created = _create(client, brand_overrides={"name": "Someone Else", "primary_color": "#112233"})
    assert created.status_code == 201
    with factory() as db:
        assert db.get(AuditJob, created.json()["job_id"]).brand_overrides is None
