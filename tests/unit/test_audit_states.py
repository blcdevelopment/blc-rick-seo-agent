"""Tripwire for the audit_jobs.status CHECK-constraint duplication.

The status vocabulary lives in three places that must agree:
  1. apps.shared.audit_states.AuditStatus (the enum / source of truth),
  2. the model's CHECK constraint, computed from JOB_STATUS_VALUES, and
  3. a FROZEN string literal in migrations/versions/20260528_0001_create_audit_tables.py.

(1) and (2) stay in sync automatically; (3) does not. This test pins the exact set so that
adding/removing/renaming a state fails loudly here, reminding you to also write a migration that
ALTERs the Postgres CHECK (otherwise SQLite/QA would accept the new value while a real Postgres
insert is rejected).
"""

from __future__ import annotations

from apps.shared.audit_states import JOB_STATUS_VALUES, TERMINAL_STATUSES, AuditStatus

# Keep this literal identical to the CHECK in migration 20260528_0001. If you change it, write
# a new Alembic migration to match.
_EXPECTED_STATUSES = (
    "queued",
    "crawling",
    "collecting_performance",
    "extracting",
    "scoring",
    "commenting",
    "validating",
    "rendering",
    "complete",
    "failed",
)


def test_job_status_values_are_pinned_to_the_migration_literal() -> None:
    assert JOB_STATUS_VALUES == _EXPECTED_STATUSES


def test_job_status_values_match_the_enum() -> None:
    assert tuple(status.value for status in AuditStatus) == JOB_STATUS_VALUES


def test_terminal_statuses() -> None:
    assert {"complete", "failed"} == TERMINAL_STATUSES
