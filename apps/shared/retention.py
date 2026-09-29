"""Storage retention cleanup for locally-stored audit artifacts.

Reports, screenshots, and tool exports are kept on the local filesystem (no object
storage). Nothing prunes them, so they grow without bound. ``cleanup_storage`` deletes
artifacts older than ``settings.storage_retention_days``. There is no in-app scheduler;
run ``scripts/cleanup_storage.py`` from cron on the host (see DEPLOYMENT.md).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.shared.config import Settings

# Tracked placeholders that keep otherwise-empty storage dirs in git; never delete.
_KEEP_FILES = {".gitkeep"}


@dataclass
class CleanupResult:
    status: str
    retention_days: int
    cutoff: str | None = None
    removed_files: int = 0
    removed_dirs: int = 0
    freed_bytes: int = 0
    dry_run: bool = False
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "retention_days": self.retention_days,
            "cutoff": self.cutoff,
            "removed_files": self.removed_files,
            "removed_dirs": self.removed_dirs,
            "freed_bytes": self.freed_bytes,
            "dry_run": self.dry_run,
            "details": self.details,
        }


def _is_older(path: Path, cutoff_ts: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff_ts
    except OSError:
        return False


def cleanup_storage(
    settings: Settings,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> CleanupResult:
    """Delete reports / tool exports / screenshot job-dirs older than the retention
    window. ``storage_retention_days <= 0`` disables cleanup. Pass ``now`` to make the
    cutoff deterministic in tests."""
    retention_days = settings.storage_retention_days
    if retention_days <= 0:
        return CleanupResult(status="disabled", retention_days=retention_days)

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()
    result = CleanupResult(
        status="ok",
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        dry_run=dry_run,
    )

    # Flat artifact trees: reports/ (pdf+docx) and tool_exports/ (csv subtrees).
    for base in (settings.local_report_storage_dir, settings.local_tool_export_storage_dir):
        base = Path(base)
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_dir() or path.name in _KEEP_FILES:
                continue
            if not _is_older(path, cutoff_ts):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    continue
            result.removed_files += 1
            result.freed_bytes += size
            result.details.append(str(path))
        if not dry_run:
            _prune_empty_dirs(base)

    # Screenshots are one subdirectory per audit; drop a whole job dir once its newest
    # file is older than the cutoff.
    screenshots = Path(settings.local_screenshot_storage_dir)
    if screenshots.exists():
        for job_dir in sorted(p for p in screenshots.iterdir() if p.is_dir()):
            files = [p for p in job_dir.rglob("*") if p.is_file() and p.name not in _KEEP_FILES]
            if not files:
                continue
            newest = max(p.stat().st_mtime for p in files)
            if newest >= cutoff_ts:
                continue
            size = sum(p.stat().st_size for p in files)
            if not dry_run:
                shutil.rmtree(job_dir, ignore_errors=True)
            result.removed_dirs += 1
            result.freed_bytes += size
            result.details.append(str(job_dir))

    return result


def _prune_empty_dirs(base: Path) -> None:
    """Remove now-empty subdirectories of ``base`` (deepest first). Never removes
    ``base`` itself or dirs still holding files (e.g. a kept .gitkeep)."""
    for path in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
        if any(path.iterdir()):
            continue
        try:
            path.rmdir()
        except OSError:
            continue
