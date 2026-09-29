"""Lightweight operational metrics — no Prometheus/Grafana stack needed for one VM.

``collect_metrics`` aggregates audit job + local-storage stats from the DB and filesystem.
Surfaced by ``GET /metrics`` (gated) and consumed by ``scripts/health_alert.py``. Windowed
counts are computed in Python (not SQL) so they are tz-safe across Postgres (tz-aware) and the
SQLite used by tests/QA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.shared.audit_states import TERMINAL_STATUSES, AuditStatus
from apps.shared.config import Settings
from apps.shared.models import AuditJob

JsonDict = dict[str, Any]
_KEEP_FILES = {".gitkeep"}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def collect_metrics(db: Session, settings: Settings, *, now: datetime | None = None) -> JsonDict:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)

    rows = db.execute(
        select(
            AuditJob.status,
            AuditJob.started_at,
            AuditJob.completed_at,
            AuditJob.created_at,
        )
    ).all()

    by_status: dict[str, int] = {}
    completed_24h = 0
    failed_24h = 0
    in_progress = 0
    durations: list[float] = []
    oldest_in_progress_minutes: int | None = None

    for status, started_at, completed_at, created_at in rows:
        by_status[status] = by_status.get(status, 0) + 1
        completed = _as_utc(completed_at)
        if status not in TERMINAL_STATUSES:
            in_progress += 1
            ref = _as_utc(started_at) or _as_utc(created_at)
            if ref is not None:
                age_min = int((now - ref).total_seconds() // 60)
                if oldest_in_progress_minutes is None or age_min > oldest_in_progress_minutes:
                    oldest_in_progress_minutes = age_min
        if completed is not None and completed >= since:
            if status == AuditStatus.COMPLETE.value:
                completed_24h += 1
                started = _as_utc(started_at)
                if started is not None:
                    durations.append((completed - started).total_seconds())
            elif status == AuditStatus.FAILED.value:
                failed_24h += 1

    reports_dir = Path(settings.local_report_storage_dir)
    report_files = (
        [p for p in reports_dir.rglob("*") if p.is_file() and p.name not in _KEEP_FILES]
        if reports_dir.exists()
        else []
    )

    return {
        "generated_at": now.isoformat(),
        "audits": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "completed_24h": completed_24h,
            "failed_24h": failed_24h,
            "in_progress": in_progress,
            "oldest_in_progress_minutes": oldest_in_progress_minutes,
            "avg_completion_seconds": (
                round(sum(durations) / len(durations), 1) if durations else None
            ),
        },
        "storage": {
            "report_files": len(report_files),
            "report_bytes": sum(_safe_size(p) for p in report_files),
        },
    }
