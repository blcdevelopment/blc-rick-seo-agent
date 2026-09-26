"""Public, read-only access to a report via a signed share token.

This router is intentionally NOT gated by Clerk: a client receives a link with a
random, time-limited token and can view/download the report without an account. Access
is granted only when the token matches, has not been revoked (token set to NULL), and
has not expired. Tokens are 32 bytes of URL-safe randomness, so they are not guessable.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.deps import get_db_session
from apps.api.schemas.audits import SharedReportResponse
from apps.shared.models import AuditJob
from apps.worker.stages.report_payload import compose_report_payload
from apps.worker.stages.social.report import compose_social_report_payload

router = APIRouter(prefix="/shared", tags=["shared"])
DbSession = Annotated[Session, Depends(get_db_session)]


def _job_for_token(db: Session, token: str) -> AuditJob:
    job = db.scalars(select(AuditJob).where(AuditJob.share_token == token)).first()
    if job is None or not job.share_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found or revoked."
        )
    expires_at = job.share_expires_at
    if expires_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found or revoked."
        )
    # SQLite returns naive datetimes; treat a missing tz as UTC for the comparison.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Share link has expired.")
    return job


@router.get("/{token}", response_model=SharedReportResponse)
def get_shared_report(token: str, db: DbSession) -> SharedReportResponse:
    job = _job_for_token(db, token)
    if job.result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report is not available for this link."
        )
    audit_type = job.audit_type or "website"
    report = None
    social_report = None
    if audit_type == "social":
        social_report = compose_social_report_payload(job, job.result)
    else:
        report = compose_report_payload(job, job.result)
    return SharedReportResponse(
        job_id=job.id,
        url=job.url,
        audit_type=audit_type,
        completed_at=job.completed_at,
        report=report,
        social_report=social_report,
    )


@router.get("/{token}/report")
def get_shared_report_pdf(token: str, db: DbSession) -> FileResponse:
    job = _job_for_token(db, token)
    if not job.result or not job.result.pdf_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report is not available for this link."
        )
    pdf_path = Path(job.result.pdf_path)
    if not pdf_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report file is missing from storage."
        )
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"audit-report-{job.id}.pdf",
    )
