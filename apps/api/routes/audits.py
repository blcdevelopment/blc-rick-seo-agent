import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from apps.api.auth import require_user
from apps.api.deps import get_db_session
from apps.api.schemas.audits import (
    AuditCreateRequest,
    AuditCreateResponse,
    AuditDetailResponse,
    AuditEnrichmentResponse,
    AuditListItem,
    AuditListResponse,
    AuditShareResponse,
    AuditShareRevokeResponse,
    AuditStatusResponse,
)
from apps.shared.audit_states import AuditStatus
from apps.shared.config import get_settings
from apps.shared.models import AuditJob
from apps.worker.stages.docx_renderer import render_audit_docx
from apps.worker.stages.report_payload import compose_report_payload
from apps.worker.stages.social.extractor import profile_link_from_handle
from apps.worker.stages.social.report import compose_social_report_payload

# Every audit endpoint requires a valid Clerk session (no-op when CLERK_ISSUER is unset).
router = APIRouter(prefix="/audits", tags=["audits"], dependencies=[Depends(require_user)])
DbSession = Annotated[Session, Depends(get_db_session)]
AuditLimit = Annotated[int, Query(ge=1, le=100)]
AuditOffset = Annotated[int, Query(ge=0)]


def _social_primary_url(handles: dict[str, str]) -> str:
    """A real, displayable profile URL stored as the social job's `url` (kept NOT NULL).

    A handle supplied AS a profile link (the new-audit form accepts links — scheme'd,
    protocol-relative, or scheme-less) is kept verbatim via the extractor's single
    URL-shaped-handle detector; nesting it under the platform host would mint a
    doubled-domain URL."""
    for platform in ("instagram", "facebook", "youtube"):
        handle = handles.get(platform)
        if handle:
            link = profile_link_from_handle(handle)
            if link is not None:
                return link
            cleaned = handle.lstrip("@").strip("/")
            if platform == "youtube":
                return f"https://www.youtube.com/@{cleaned}"
            host = "instagram.com" if platform == "instagram" else "facebook.com"
            return f"https://www.{host}/{cleaned}/"
    platform, handle = next(iter(handles.items()))
    return (
        profile_link_from_handle(handle)
        or f"https://{platform}.com/{handle.strip().lstrip('@').strip('/')}"
    )


def _report_available(job: AuditJob) -> bool:
    return bool(job.result and job.result.pdf_path and Path(job.result.pdf_path).exists())


def _report_recorded(job: AuditJob) -> bool:
    # Cheap, DB-only check for list views: avoids a filesystem stat per row (which would
    # become a network round-trip per row once reports move to object storage). The
    # download endpoint remains the source of truth and 404s if the file is gone.
    return bool(job.result and job.result.pdf_path)


def _overall_score(job: AuditJob) -> int | None:
    """The combined-audit Overall Lead-Gen Readiness score (stored under
    score_breakdown.overall_readiness). None for website/social-only audits."""
    breakdown = getattr(job.result, "score_breakdown", None)
    if isinstance(breakdown, dict):
        overall = breakdown.get("overall_readiness")
        if isinstance(overall, dict):
            return overall.get("score")
    return None


def _docx_path(job: AuditJob) -> Path | None:
    if not job.result:
        return None
    metadata = job.result.report_metadata or {}
    path = metadata.get("docx_path") if isinstance(metadata, dict) else None
    return Path(path) if isinstance(path, str) and path else None


def _status_response(job: AuditJob) -> AuditStatusResponse:
    return AuditStatusResponse(
        job_id=job.id,
        url=job.url,
        status=job.status,
        current_stage=job.current_stage,
        progress_pct=job.progress_pct,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        report_available=_report_available(job),
    )


def _detail_response(job: AuditJob) -> AuditDetailResponse:
    audit_type = job.audit_type or "website"
    result = job.result
    report = None
    social_report = None
    if result is not None:
        if audit_type == "social":
            social_report = compose_social_report_payload(job, result)
        else:
            report = compose_report_payload(job, result)
    return AuditDetailResponse(
        job_id=job.id,
        url=job.url,
        audit_type=audit_type,
        niche=job.niche,
        target_audience=job.target_audience,
        social_handles=job.social_handles,
        status=job.status,
        current_stage=job.current_stage,
        progress_pct=job.progress_pct,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        report_available=_report_available(job),
        seo_score=result.seo_score if result else None,
        uxui_score=result.uxui_score if result else None,
        lead_gen_score=result.lead_gen_score if result else None,
        social_score=result.social_score if result else None,
        overall_score=_overall_score(job),
        report=report,
        social_report=social_report,
    )


def _list_item(job: AuditJob) -> AuditListItem:
    result = job.result
    return AuditListItem(
        job_id=job.id,
        url=job.url,
        audit_type=job.audit_type or "website",
        status=job.status,
        current_stage=job.current_stage,
        progress_pct=job.progress_pct,
        created_at=job.created_at,
        completed_at=job.completed_at,
        seo_score=result.seo_score if result else None,
        uxui_score=result.uxui_score if result else None,
        lead_gen_score=result.lead_gen_score if result else None,
        social_score=result.social_score if result else None,
        overall_score=_overall_score(job),
        report_available=_report_recorded(job),
    )


@router.post("", response_model=AuditCreateResponse, status_code=status.HTTP_201_CREATED)
def create_audit(
    payload: AuditCreateRequest,
    db: DbSession,
) -> AuditCreateResponse:
    settings = get_settings()
    brand_overrides = None
    if payload.brand_overrides is not None:
        brand_overrides = payload.brand_overrides.model_dump(exclude_none=True) or None

    if payload.audit_type == "social":
        handles = {k: v for k, v in (payload.social_handles or {}).items() if v}
        job = AuditJob(
            url=_social_primary_url(handles),
            audit_type="social",
            social_handles=handles,
            niche=payload.niche,
            target_audience=payload.target_audience,
            brand_overrides=brand_overrides,
            status=AuditStatus.QUEUED.value,
            current_stage="Queued",
            progress_pct=0,
        )
    elif payload.audit_type == "combined":
        # Combined: the real website URL plus social handles. The worker runs the website pipeline
        # then the social audit and renders ONE report with both + the overall readiness score.
        handles = {k: v for k, v in (payload.social_handles or {}).items() if v}
        job = AuditJob(
            url=str(payload.url),
            audit_type="combined",
            social_handles=handles,
            niche=payload.niche,
            target_audience=payload.target_audience,
            brand_overrides=brand_overrides,
            status=AuditStatus.QUEUED.value,
            current_stage="Queued",
            progress_pct=0,
        )
    else:
        job = AuditJob(
            url=str(payload.url),
            audit_type="website",
            niche=payload.niche,
            target_audience=payload.target_audience,
            brand_overrides=brand_overrides,
            status=AuditStatus.QUEUED.value,
            current_stage="Queued",
            progress_pct=0,
        )
    db.add(job)
    db.commit()
    db.refresh(job)

    if settings.audit_enqueue_enabled:
        try:
            from apps.worker.tasks import run_audit

            run_audit.delay(str(job.id))
        except Exception as exc:
            job.status = AuditStatus.FAILED.value
            job.current_stage = "Failed to enqueue"
            job.progress_pct = 100
            job.error_message = str(exc)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "Audit job was created but could not be enqueued.",
                    "job_id": str(job.id),
                    "error": str(exc),
                },
            ) from exc

    return AuditCreateResponse(
        job_id=job.id,
        status=job.status,
        status_url=f"/audits/{job.id}/status",
    )


@router.get("", response_model=AuditListResponse)
def list_audits(
    db: DbSession,
    limit: AuditLimit = 25,
    offset: AuditOffset = 0,
) -> AuditListResponse:
    jobs = db.scalars(
        select(AuditJob)
        .options(selectinload(AuditJob.result))
        .order_by(AuditJob.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return AuditListResponse(audits=[_list_item(job) for job in jobs])


@router.get("/{job_id}/status", response_model=AuditStatusResponse)
def get_audit_status(job_id: UUID, db: DbSession) -> AuditStatusResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    return _status_response(job)


@router.get("/{job_id}", response_model=AuditDetailResponse)
def get_audit_detail(job_id: UUID, db: DbSession) -> AuditDetailResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    return _detail_response(job)


@router.post("/{job_id}/rerun-enrichment", response_model=AuditEnrichmentResponse)
def rerun_audit_enrichment(job_id: UUID, db: DbSession) -> AuditEnrichmentResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    if job.result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Audit enrichment can only be rerun after baseline audit facts exist.",
        )

    settings = get_settings()
    if settings.audit_enqueue_enabled:
        try:
            from apps.worker.tasks import rerun_external_enrichment

            job.status = AuditStatus.EXTRACTING.value
            job.current_stage = "Queued external SEO enrichment"
            job.progress_pct = 70
            job.error_message = None
            db.commit()
            rerun_external_enrichment.delay(str(job.id))
        except Exception as exc:
            # The audit already has a complete result and report (enforced by the
            # 409 above) — a broker hiccup while queueing the OPTIONAL enrichment
            # must not flip it to FAILED.
            job.status = AuditStatus.COMPLETE.value
            job.current_stage = "Enrichment could not be queued; previous report kept"
            job.progress_pct = 100
            job.error_message = str(exc)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "Audit enrichment could not be enqueued.",
                    "job_id": str(job.id),
                    "error": str(exc),
                },
            ) from exc
    else:
        from apps.worker.tasks import rerun_external_enrichment_for_audit

        rerun_external_enrichment_for_audit(str(job.id))
        db.refresh(job)

    return AuditEnrichmentResponse(
        job_id=job.id,
        status=job.status,
        current_stage=job.current_stage,
        message="External SEO enrichment has been queued or started.",
    )


@router.post("/{job_id}/rerun-ai-visibility", response_model=AuditEnrichmentResponse)
def rerun_audit_ai_visibility(job_id: UUID, db: DbSession) -> AuditEnrichmentResponse:
    """On-demand AI Visibility (Semrush) enrichment for a completed audit.

    Mirrors ``rerun-enrichment``: 404 if no job, 409 if there is no baseline result yet, 503 if the
    optional work can't be enqueued (the existing report is kept COMPLETE in that case). The bot
    logs into Semrush, screenshots the AI Visibility dashboard, and re-renders — a failure/skip
    leaves the previous report byte-identical (AI visibility is presentation only)."""
    # Refuse up front when the enrichment is switched off (AI_VISIBILITY_ENABLED=false). The worker
    # would skip anyway (the collector checks the same flag before any network), but a clear 409
    # here means the operator's "Refresh AI Visibility" click says WHY instead of spinning through
    # a task that stores nothing — and no Celery task is enqueued at all while the kill-switch is
    # thrown (e.g. while the Semrush account cools down from a lockout).
    if not get_settings().ai_visibility_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="AI visibility is disabled (AI_VISIBILITY_ENABLED=false); enable it to refresh.",
        )
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    if job.result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="AI visibility enrichment can only run after baseline audit facts exist.",
        )

    settings = get_settings()
    if settings.audit_enqueue_enabled:
        try:
            from apps.worker.tasks import rerun_ai_visibility

            job.status = AuditStatus.EXTRACTING.value
            job.current_stage = "Queued AI visibility enrichment"
            job.progress_pct = 80
            job.error_message = None
            db.commit()
            rerun_ai_visibility.delay(str(job.id))
        except Exception as exc:
            # The audit already has a complete result and report — a broker hiccup while queueing
            # the OPTIONAL enrichment must not flip it to FAILED.
            job.status = AuditStatus.COMPLETE.value
            job.current_stage = "AI visibility could not be queued; previous report kept"
            job.progress_pct = 100
            job.error_message = str(exc)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "AI visibility enrichment could not be enqueued.",
                    "job_id": str(job.id),
                    "error": str(exc),
                },
            ) from exc
    else:
        from apps.worker.tasks import rerun_ai_visibility_for_audit

        rerun_ai_visibility_for_audit(str(job.id))
        db.refresh(job)

    return AuditEnrichmentResponse(
        job_id=job.id,
        status=job.status,
        current_stage=job.current_stage,
        message="AI visibility enrichment has been queued or started.",
    )


@router.get("/{job_id}/report")
def get_audit_report(job_id: UUID, db: DbSession) -> FileResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    if not job.result or not job.result.pdf_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PDF report has not been generated for this audit yet.",
        )

    pdf_path = Path(job.result.pdf_path)
    if not pdf_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PDF report file is missing from local storage.",
        )

    kind = "social" if (job.audit_type or "website") == "social" else "website"
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"blc-{kind}-audit-{job.id}.pdf",
    )


@router.get("/{job_id}/docx")
def get_audit_docx(job_id: UUID, db: DbSession) -> FileResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    if not job.result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DOCX report has not been generated for this audit yet.",
        )

    docx_path = _docx_path(job)
    if docx_path is None or not docx_path.exists():
        try:
            docx_result = render_audit_docx(job, job.result, get_settings())
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The DOCX export could not be generated; the PDF report is unaffected.",
            ) from exc
        metadata = dict(job.result.report_metadata or {})
        raw_exports = metadata.get("exports")
        exports = dict(raw_exports) if isinstance(raw_exports, dict) else {}
        exports["docx"] = docx_result.report_metadata
        metadata["exports"] = exports
        metadata["docx_path"] = docx_result.docx_path
        metadata["docx_size_bytes"] = docx_result.size_bytes
        job.result.report_metadata = metadata
        db.commit()
        db.refresh(job.result)
        docx_path = Path(docx_result.docx_path)

    return FileResponse(
        docx_path,
        media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        filename=f"blc-website-audit-{job.id}.docx",
    )


@router.post("/{job_id}/share", response_model=AuditShareResponse)
def create_share_link(job_id: UUID, db: DbSession) -> AuditShareResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    if not _report_recorded(job):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A report must be generated before the audit can be shared.",
        )

    settings = get_settings()
    job.share_token = secrets.token_urlsafe(32)
    job.share_expires_at = datetime.now(UTC) + timedelta(days=settings.share_link_ttl_days)
    db.commit()
    db.refresh(job)
    return AuditShareResponse(
        job_id=job.id,
        share_token=job.share_token,
        share_expires_at=job.share_expires_at,
        report_path=f"/shared/{job.share_token}/report",
    )


@router.delete("/{job_id}/share", response_model=AuditShareRevokeResponse)
def revoke_share_link(job_id: UUID, db: DbSession) -> AuditShareRevokeResponse:
    job = db.get(AuditJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit job not found.")
    job.share_token = None
    job.share_expires_at = None
    db.commit()
    return AuditShareRevokeResponse(job_id=job.id, shared=False)
