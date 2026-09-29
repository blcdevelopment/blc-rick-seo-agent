"""add external seo facts and google search console connections

Revision ID: 20260611_0002
Revises: 20260528_0001
Create Date: 2026-06-11 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260611_0002"
down_revision: str | None = "20260528_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_results",
        sa.Column(
            "external_seo_facts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("audit_results", "external_seo_facts", server_default=None)

    op.create_table(
        "google_search_console_connections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("account_email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_gsc_connections_created",
        "google_search_console_connections",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_gsc_connections_created",
        table_name="google_search_console_connections",
    )
    op.drop_table("google_search_console_connections")
    op.drop_column("audit_results", "external_seo_facts")
