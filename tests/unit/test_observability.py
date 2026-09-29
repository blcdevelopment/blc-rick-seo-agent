import os
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.shared.audit_states import AuditStatus
from apps.shared.config import Settings
from apps.shared.metrics import collect_metrics
from apps.shared.models import AuditJob, Base
from scripts.backup_db import pg_dump_url, prune_backups
from scripts.health_alert import evaluate_alerts

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _job(status, *, created=None, started=None, completed=None) -> AuditJob:
    return AuditJob(
        url="https://example.com/",
        status=status,
        progress_pct=100 if status == AuditStatus.COMPLETE.value else 50,
        created_at=created or NOW,
        started_at=started,
        completed_at=completed,
    )


# --- metrics ---------------------------------------------------------------


def test_collect_metrics(tmp_path) -> None:
    factory = _factory()
    with factory() as db:
        db.add_all(
            [
                _job(  # completed in last 24h, 300s duration
                    AuditStatus.COMPLETE.value,
                    started=NOW - timedelta(hours=1, minutes=5),
                    completed=NOW - timedelta(hours=1),
                ),
                _job(AuditStatus.FAILED.value, completed=NOW - timedelta(hours=2)),  # failed in 24h
                _job(  # in progress ~90 min
                    AuditStatus.CRAWLING.value,
                    created=NOW - timedelta(minutes=90),
                    started=NOW - timedelta(minutes=90),
                ),
                _job(  # completed 30h ago — outside the 24h window
                    AuditStatus.COMPLETE.value,
                    started=NOW - timedelta(hours=31),
                    completed=NOW - timedelta(hours=30),
                ),
            ]
        )
        db.commit()
        settings = Settings(_env_file=None, local_report_storage_dir=tmp_path)
        m = collect_metrics(db, settings, now=NOW)

    a = m["audits"]
    assert a["total"] == 4
    assert a["completed_24h"] == 1
    assert a["failed_24h"] == 1
    assert a["in_progress"] == 1
    assert a["avg_completion_seconds"] == 300.0
    assert a["oldest_in_progress_minutes"] == 90
    assert m["storage"]["report_files"] == 0


# --- alerts ----------------------------------------------------------------


def test_evaluate_alerts_failed_threshold() -> None:
    alerts = evaluate_alerts(
        {"audits": {"failed_24h": 7, "oldest_in_progress_minutes": 10}},
        failed_threshold=5,
        stuck_minutes=60,
    )
    assert len(alerts) == 1 and "failed" in alerts[0]


def test_evaluate_alerts_stuck_job() -> None:
    alerts = evaluate_alerts(
        {"audits": {"failed_24h": 0, "oldest_in_progress_minutes": 120}},
        failed_threshold=5,
        stuck_minutes=60,
    )
    assert len(alerts) == 1 and "in progress" in alerts[0]


def test_evaluate_alerts_clean() -> None:
    alerts = evaluate_alerts(
        {"audits": {"failed_24h": 1, "oldest_in_progress_minutes": 5}},
        failed_threshold=5,
        stuck_minutes=60,
    )
    assert alerts == []


# --- backups ---------------------------------------------------------------


def test_pg_dump_url_strips_driver() -> None:
    assert pg_dump_url("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert pg_dump_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"  # unchanged


def test_prune_backups_removes_old_keeps_recent(tmp_path) -> None:
    old = tmp_path / "blc-db-20260101T000000Z.sql.gz"
    recent = tmp_path / "blc-db-20260624T000000Z.sql.gz"
    other = tmp_path / "keep-me.txt"  # not a backup file — must be left alone
    for p in (old, recent, other):
        p.write_bytes(b"x")
    # Make `old` 40 days old by mtime.
    old_ts = time.time() - 40 * 86400
    os.utime(old, (old_ts, old_ts))

    removed = prune_backups(tmp_path, retention_days=14)
    assert removed == [old]
    assert not old.exists()
    assert recent.exists()
    assert other.exists()


def test_prune_backups_disabled_when_zero(tmp_path) -> None:
    (tmp_path / "blc-db-20200101T000000Z.sql.gz").write_bytes(b"x")
    assert prune_backups(tmp_path, retention_days=0) == []


# --- /metrics route --------------------------------------------------------


def test_metrics_endpoint_returns_json(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from apps.api.deps import get_db_session
    from apps.api.main import app
    from apps.api.routes import metrics as metrics_route

    factory = _factory()
    with factory() as db:
        db.add(_job(AuditStatus.COMPLETE.value))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        metrics_route,
        "get_settings",
        lambda: Settings(_env_file=None, local_report_storage_dir=tmp_path),
    )
    app.dependency_overrides[get_db_session] = override_db
    try:
        resp = TestClient(app).get("/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert {"audits", "storage", "generated_at"} <= set(body)
        assert body["audits"]["total"] == 1
    finally:
        app.dependency_overrides.clear()


# --- HTTP client log hygiene -------------------------------------------------


def test_quiet_http_client_logs_drops_info_request_lines() -> None:
    import logging

    from apps.shared.observability import quiet_http_client_logs

    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    previous = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.NOTSET)
        quiet_http_client_logs()
        for logger in loggers:
            # "HTTP Request: GET <full url>" is an INFO record — it must no longer be emitted.
            assert not logger.isEnabledFor(logging.INFO)
            assert logger.isEnabledFor(logging.WARNING)
    finally:
        for logger, level in zip(loggers, previous, strict=True):
            logger.setLevel(level)
