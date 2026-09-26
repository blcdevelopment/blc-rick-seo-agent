"""add advisory accessibility_facts column

Revision ID: 20260625_0005
Revises: 20260623_0004
Create Date: 2026-06-25 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260625_0005"
down_revision: str | None = "20260623_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Advisory-only axe-core accessibility findings (P2-15b). Nullable + additive; populated
    # only when the opt-in pass runs. Never read by scoring.
    op.add_column(
        "audit_results",
        sa.Column("accessibility_facts", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_results", "accessibility_facts")
