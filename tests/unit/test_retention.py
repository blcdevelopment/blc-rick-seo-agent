import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.shared.config import Settings
from apps.shared.retention import cleanup_storage


def _settings(tmp_path: Path, days: int) -> Settings:
    return Settings(
        local_report_storage_dir=tmp_path / "reports",
        local_screenshot_storage_dir=tmp_path / "screenshots",
        local_tool_export_storage_dir=tmp_path / "tool_exports",
        storage_retention_days=days,
    )


def _age(path: Path, days: int, now: datetime) -> None:
    ts = (now - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def test_cleanup_removes_old_keeps_new_and_gitkeep(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, tzinfo=UTC)

    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / ".gitkeep").write_text("")
    old_pdf = reports / "old.pdf"
    old_pdf.write_text("x")
    new_pdf = reports / "new.pdf"
    new_pdf.write_text("y")

    exports = tmp_path / "tool_exports" / "screaming_frog" / "job-old"
    exports.mkdir(parents=True)
    old_csv = exports / "internal_all.csv"
    old_csv.write_text("a,b,c")

    screenshots = tmp_path / "screenshots"
    old_job = screenshots / "job-old"
    old_job.mkdir(parents=True)
    (old_job / "home.png").write_text("z")
    new_job = screenshots / "job-new"
    new_job.mkdir(parents=True)
    (new_job / "home.png").write_text("z")

    _age(old_pdf, 200, now)
    _age(old_csv, 200, now)
    _age(old_job / "home.png", 200, now)
    _age(new_pdf, 1, now)
    _age(new_job / "home.png", 1, now)

    result = cleanup_storage(_settings(tmp_path, 90), now=now)

    assert result.status == "ok"
    assert not old_pdf.exists()
    assert new_pdf.exists()
    assert (reports / ".gitkeep").exists()
    assert not old_csv.exists()
    assert not old_job.exists()
    assert new_job.exists()
    assert result.removed_files == 2  # old.pdf + old internal_all.csv
    assert result.removed_dirs == 1  # old screenshot job dir


def test_cleanup_disabled_when_retention_zero(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    old = reports / "old.pdf"
    old.write_text("x")
    ancient = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(old, (ancient, ancient))

    result = cleanup_storage(_settings(tmp_path, 0))

    assert result.status == "disabled"
    assert old.exists()


def test_cleanup_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, tzinfo=UTC)
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    old_pdf = reports / "old.pdf"
    old_pdf.write_text("x")
    _age(old_pdf, 200, now)

    result = cleanup_storage(_settings(tmp_path, 90), now=now, dry_run=True)

    assert result.dry_run is True
    assert result.removed_files == 1
    assert old_pdf.exists()  # not actually deleted
