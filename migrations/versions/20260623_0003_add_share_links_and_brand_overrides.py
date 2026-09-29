"""add share links and brand overrides to audit_jobs

Revision ID: 20260623_0003
Revises: 20260611_0002
Create Date: 2026-06-23 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260623_0003"
down_revision: str | None = "20260611_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_jobs", sa.Column("brand_overrides", postgresql.JSONB(), nullable=True))
    op.add_column("audit_jobs", sa.Column("share_token", sa.String(length=64), nullable=True))
    op.add_column(
        "audit_jobs",
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_audit_jobs_share_token", "audit_jobs", ["share_token"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_audit_jobs_share_token", table_name="audit_jobs")
    op.drop_column("audit_jobs", "share_expires_at")
    op.drop_column("audit_jobs", "share_token")
    op.drop_column("audit_jobs", "brand_overrides")
