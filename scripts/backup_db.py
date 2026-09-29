"""PostgreSQL backup — run from cron on the host.

Runs ``pg_dump`` against ``DATABASE_URL`` into a gzipped, timestamped file under
``BACKUP_STORAGE_DIR`` and prunes backups older than ``BACKUP_RETENTION_DAYS``.

    30 2 * * *  cd /app && python scripts/backup_db.py >> /var/log/blc-backup.log 2>&1

In the Docker stack run it inside the api/worker container (which has pg_dump and the
DATABASE_URL), or on the host with PG client tools installed. Reports/screenshots stay on
the local FS and are managed by scripts/cleanup_storage.py — this backs up the database
(the audit_jobs / audit_results rows), which is the part that can't be regenerated.
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.shared.config import Settings, get_settings

_BACKUP_GLOB = "blc-db-*.sql.gz"


def pg_dump_url(database_url: str) -> str:
    """pg_dump wants a libpq URI — strip the SQLAlchemy driver suffix (+psycopg/+psycopg2)."""
    return database_url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def prune_backups(
    backup_dir: Path, retention_days: int, *, now: datetime | None = None
) -> list[Path]:
    """Delete ``blc-db-*.sql.gz`` older than the retention window. 0 disables. Returns removed."""
    if retention_days <= 0 or not backup_dir.exists():
        return []
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).timestamp()
    removed: list[Path] = []
    for path in sorted(backup_dir.glob(_BACKUP_GLOB)):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def run_backup(settings: Settings, *, now: datetime | None = None) -> dict:
    backup_dir = Path(settings.backup_storage_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"blc-db-{stamp}.sql.gz"

    try:
        proc = subprocess.run(  # noqa: S603 - pg_dump path + our own URL, not user input
            [settings.pg_dump_path, pg_dump_url(settings.database_url)],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return {"status": "failed", "reason": f"{settings.pg_dump_path} not found on PATH"}
    if proc.returncode != 0:
        return {
            "status": "failed",
            "reason": proc.stderr.decode("utf-8", "replace").strip()[:500] or "pg_dump failed",
        }

    target.write_bytes(gzip.compress(proc.stdout))
    pruned = prune_backups(backup_dir, settings.backup_retention_days, now=now)
    return {
        "status": "ok",
        "backup": str(target),
        "bytes": target.stat().st_size,
        "pruned": len(pruned),
        "retention_days": settings.backup_retention_days,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the PostgreSQL database via pg_dump.")
    parser.add_argument("--days", type=int, default=None, help="Override BACKUP_RETENTION_DAYS.")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.days is not None:
        settings = settings.model_copy(update={"backup_retention_days": args.days})

    result = run_backup(settings)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
