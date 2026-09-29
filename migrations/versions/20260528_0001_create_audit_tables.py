"""create audit jobs and results tables

Revision ID: 20260528_0001
Revises:
Create Date: 2026-05-28 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260528_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "audit_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("niche", sa.String(length=255), nullable=True),
        sa.Column("target_audience", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("current_stage", sa.String(length=120), nullable=True),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_audit_jobs_progress_pct",
        ),
        # This status list is a FROZEN duplicate of apps.shared.audit_states.JOB_STATUS_VALUES
        # (the model computes its CHECK from that enum). Adding/removing a lifecycle state
        # requires a NEW migration to ALTER this constraint; tests/unit/test_audit_states.py
        # is the tripwire that flags the drift.
        sa.CheckConstraint(
            "status IN ('queued', 'crawling', 'collecting_performance', 'extracting', "
            "'scoring', 'commenting', 'validating', 'rendering', 'complete', 'failed')",
            name="ck_audit_jobs_status",
        ),
    )
    op.create_index("idx_audit_jobs_status", "audit_jobs", ["status"])
    op.create_index("idx_audit_jobs_created", "audit_jobs", ["created_at"])

    op.create_table(
        "audit_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("seo_score", sa.Integer(), nullable=False),
        sa.Column("uxui_score", sa.Integer(), nullable=False),
        sa.Column("lead_gen_score", sa.Integer(), nullable=False),
        sa.Column("crawled_pages", postgresql.JSONB(), nullable=False),
        sa.Column("seo_facts", postgresql.JSONB(), nullable=False),
        sa.Column("uxui_facts", postgresql.JSONB(), nullable=False),
        sa.Column("psi_facts", postgresql.JSONB(), nullable=False),
        sa.Column("score_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("commentary", postgresql.JSONB(), nullable=False),
        sa.Column("validation_log", postgresql.JSONB(), nullable=False),
        sa.Column("report_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("pdf_path", sa.Text(), nullable=True),
        sa.Column("rubric_version", sa.String(length=80), nullable=False),
        sa.Column("llm_model", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_audit_results_job", "audit_results", ["job_id"])


def downgrade() -> None:
    op.drop_index("idx_audit_results_job", table_name="audit_results")
    op.drop_table("audit_results")
    op.drop_index("idx_audit_jobs_created", table_name="audit_jobs")
    op.drop_index("idx_audit_jobs_status", table_name="audit_jobs")
    op.drop_table("audit_jobs")
