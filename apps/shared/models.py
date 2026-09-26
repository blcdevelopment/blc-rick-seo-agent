from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, JSON, TypeDecorator

from apps.shared.audit_states import JOB_STATUS_VALUES, AuditStatus


class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def json_type():
    return JSON().with_variant(JSONB, "postgresql")


JsonDict = dict[str, Any]


class Base(DeclarativeBase):
    pass


class AuditJob(Base):
    __tablename__ = "audit_jobs"
    __table_args__ = (
        CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_audit_jobs_progress_pct",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in JOB_STATUS_VALUES)})",
            name="ck_audit_jobs_status",
        ),
        Index("idx_audit_jobs_status", "status"),
        Index("idx_audit_jobs_created", "created_at"),
        Index("idx_audit_jobs_share_token", "share_token", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    niche: Mapped[str | None] = mapped_column(String(255))
    target_audience: Mapped[str | None] = mapped_column(String(255))
    # "website" (default) or "social" — selects which pipeline run_collection_audit runs.
    # Social audits are standalone (own report + Social Score; website scoring untouched).
    audit_type: Mapped[str] = mapped_column(String(20), nullable=False, default="website")
    # {platform: handle} for social audits (e.g. {"instagram": "acmebuilders"}); null for website.
    social_handles: Mapped[JsonDict | None] = mapped_column(json_type())
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=AuditStatus.QUEUED.value
    )
    current_stage: Mapped[str | None] = mapped_column(String(120))
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-client white-label branding overrides (name / colors / logo_url) applied at render.
    brand_overrides: Mapped[JsonDict | None] = mapped_column(json_type())
    # Read-only report sharing: a random token grants access to the report without a Clerk
    # session until share_expires_at. A null token means not shared / revoked.
    share_token: Mapped[str | None] = mapped_column(String(64))
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    result: Mapped[AuditResult | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


class AuditResult(Base):
    __tablename__ = "audit_results"
    __table_args__ = (Index("idx_audit_results_job", "job_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("audit_jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Website-audit scores are nullable so a SOCIAL audit result can leave them empty and
    # populate social_score instead. A website audit always sets all three.
    seo_score: Mapped[int | None] = mapped_column(Integer)
    uxui_score: Mapped[int | None] = mapped_column(Integer)
    lead_gen_score: Mapped[int | None] = mapped_column(Integer)
    social_score: Mapped[int | None] = mapped_column(Integer)
    social_facts: Mapped[JsonDict | None] = mapped_column(json_type())
    crawled_pages: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    seo_facts: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    uxui_facts: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    psi_facts: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    external_seo_facts: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    # Advisory-only axe-core accessibility findings (P2-15b). Nullable; populated only when the
    # opt-in pass runs. NEVER read by scoring — kept in its own column so it can't be confused
    # with the trust-gated, scored external_seo_facts.
    accessibility_facts: Mapped[JsonDict | None] = mapped_column(json_type())
    score_breakdown: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    commentary: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    validation_log: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    report_metadata: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    rubric_version: Mapped[str] = mapped_column(String(80), nullable=False)
    llm_model: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[AuditJob] = relationship(back_populates="result")


class GoogleSearchConsoleConnection(Base):
    __tablename__ = "google_search_console_connections"
    __table_args__ = (Index("idx_gsc_connections_created", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    account_email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # Google OAuth tokens. Stored unencrypted by deliberate decision: this is a single internal
    # VM where DB read access and shell access (where any encryption key would live in .env)
    # belong to the same handful of operators, so at-rest encryption with a co-located key would
    # add no real protection against that threat model — only churn. The one genuine residual
    # risk is a DB dump leaving the box, so keep backups (scripts/backup_db.py output) on-box and
    # access-controlled. Revisit if Google credentials ever need to outlive this trust boundary.
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    properties: Mapped[JsonDict] = mapped_column(json_type(), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
