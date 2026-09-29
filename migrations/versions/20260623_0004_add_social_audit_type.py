"""add social audit type, handles, and standalone social score

Revision ID: 20260623_0004
Revises: 20260623_0003
Create Date: 2026-06-23 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260623_0004"
down_revision: str | None = "20260623_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_jobs",
        sa.Column("audit_type", sa.String(length=20), nullable=False, server_default="website"),
    )
    op.alter_column("audit_jobs", "audit_type", server_default=None)
    op.add_column("audit_jobs", sa.Column("social_handles", postgresql.JSONB(), nullable=True))

    op.add_column("audit_results", sa.Column("social_score", sa.Integer(), nullable=True))
    op.add_column("audit_results", sa.Column("social_facts", postgresql.JSONB(), nullable=True))
    # Website scores become nullable so a social-audit result can leave them empty.
    op.alter_column("audit_results", "seo_score", existing_type=sa.Integer(), nullable=True)
    op.alter_column("audit_results", "uxui_score", existing_type=sa.Integer(), nullable=True)
    op.alter_column("audit_results", "lead_gen_score", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.alter_column("audit_results", "lead_gen_score", existing_type=sa.Integer(), nullable=False)
    op.alter_column("audit_results", "uxui_score", existing_type=sa.Integer(), nullable=False)
    op.alter_column("audit_results", "seo_score", existing_type=sa.Integer(), nullable=False)
    op.drop_column("audit_results", "social_facts")
    op.drop_column("audit_results", "social_score")
    op.drop_column("audit_jobs", "social_handles")
    op.drop_column("audit_jobs", "audit_type")
