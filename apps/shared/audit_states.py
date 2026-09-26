from enum import StrEnum


class AuditStatus(StrEnum):
    QUEUED = "queued"
    CRAWLING = "crawling"
    COLLECTING_PERFORMANCE = "collecting_performance"
    EXTRACTING = "extracting"
    SCORING = "scoring"
    COMMENTING = "commenting"
    VALIDATING = "validating"
    RENDERING = "rendering"
    COMPLETE = "complete"
    FAILED = "failed"


# Drives the audit_jobs.status CHECK constraint in apps/shared/models.py (computed from this
# tuple). NOTE: the same list is ALSO hardcoded as a frozen string literal in the Alembic
# migration migrations/versions/20260528_0001_create_audit_tables.py. If you add/remove/rename a
# state here you MUST write a new migration to ALTER the Postgres CHECK — the create_all/SQLite
# path (tests/QA) would accept the new value, but a real Postgres insert would be rejected.
# tests/unit/test_audit_states.py is a tripwire that fails when this set changes.
JOB_STATUS_VALUES = tuple(status.value for status in AuditStatus)
TERMINAL_STATUSES = {AuditStatus.COMPLETE.value, AuditStatus.FAILED.value}
