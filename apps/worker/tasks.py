from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from apps.shared.audit_states import AuditStatus
from apps.shared.config import SOCIAL_AUDITS_DISABLED_MESSAGE, Settings, get_settings
from apps.shared.database import SessionLocal
from apps.shared.models import AuditJob, AuditResult
from apps.worker.celery_app import celery_app
from apps.worker.stages.ai_visibility.collector import collect_ai_visibility_facts
from apps.worker.stages.benchmarking.collector import collect_benchmark_facts
from apps.worker.stages.commentary import generate_commentary, generate_social_commentary
from apps.worker.stages.crawler import (
    BOT_CHECK_BLOCKED_MESSAGE,
    CrawlResult,
    SiteBlockedError,
    crawl_site_sync,
    is_interstitial_url,
)
from apps.worker.stages.docx_renderer import DocxRenderResult, render_audit_docx
from apps.worker.stages.external_seo import collect_external_seo_facts, empty_external_seo_facts
from apps.worker.stages.extractor_seo import extract_seo_facts
from apps.worker.stages.extractor_uxui import extract_uxui_facts
from apps.worker.stages.google_search_console import (
    YOUTUBE_ANALYTICS_SCOPES,
    ensure_google_access_token,
    latest_google_connection,
)
from apps.worker.stages.grounding_validator import validate_commentary_grounding
from apps.worker.stages.pdf_renderer import PdfRenderResult, render_audit_pdf, render_social_pdf
from apps.worker.stages.psi_client import collect_pagespeed_facts
from apps.worker.stages.scoring import (
    compose_overall_readiness_score,
    score_audit,
    score_social_audit,
)
from apps.worker.stages.social.collector import collect_social_facts
from apps.worker.stages.social.discovery import discover_social_links
from apps.worker.stages.social.extractor import (
    _phone_tail,
    connected_channel_matches,
    inject_category_relevance,
    inject_google_business,
    inject_nap_consistency,
)
from apps.worker.stages.social.places_provider import collect_google_business_facts
from apps.worker.stages.social.providers import get_provider
from apps.worker.stages.social.report import compose_social_report_data
from apps.worker.stages.social.youtube_analytics_provider import (
    fetch_channel_analytics,
    fetch_own_channel,
    normalize_youtube_analytics,
)
from apps.worker.stages.technical_crawl_common import registrable_brand_label

JsonDict = dict[str, Any]
CrawlerFunc = Callable[[str, Settings, str | None], CrawlResult]
PsiCollectorFunc = Callable[[Sequence[str], Settings], JsonDict]
SocialCollectorFunc = Callable[[Settings, "dict[str, str] | None"], JsonDict]
# AI-visibility collector: (settings, *, domain, retrieved_at) -> facts. Injectable so tests never
# touch the Semrush login-bot / OpenAI (pass a fake returning a facts dict), like social_collector.
AiVisibilityCollectorFunc = Callable[..., JsonDict]


# Seconds held back after external enrichment for scoring, commentary, grounding,
# PDF/DOCX rendering, and persistence, so the whole task finishes inside the Celery
# soft time limit even when the earlier stages ran long.
_PIPELINE_TAIL_RESERVE_SECONDS = 75


def _external_enrichment_deadline(settings: Settings, task_started: float) -> float:
    """Absolute ``time.monotonic()`` deadline by which external SEO enrichment must
    stop, derived from when the task started and the configured soft time limit."""
    soft_limit = getattr(settings, "celery_task_soft_time_limit_seconds", None)
    if not isinstance(soft_limit, int | float):
        return task_started + 600.0
    return task_started + max(60.0, float(soft_limit) - _PIPELINE_TAIL_RESERVE_SECONDS)


def _collect_external_seo_safely(**kwargs: Any) -> JsonDict:
    """External enrichment must never abort an audit.

    The collectors already degrade internally (skip/failed payloads), but any
    unexpected exception here is converted into a failed payload so the audit
    keeps its deterministic core result. Celery's soft time limit is re-raised:
    once it fires the task's remaining budget is gone and the job must fail fast.
    """
    try:
        return collect_external_seo_facts(**kwargs)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        facts = empty_external_seo_facts(reason="collector_error")
        facts["status"] = "failed"
        facts["error"] = " ".join(str(exc).split())[:500]
        return facts


def _collect_accessibility_advisory_safely(
    crawl_result: CrawlResult, settings: Settings
) -> JsonDict:
    """Normalize the advisory axe-core results gathered during the crawl. Advisory-only and
    must never abort an audit (mirrors external SEO). Returns ``{}`` when the pass is disabled,
    when no page produced a result, or on any normalization error. NEVER feeds scoring."""
    if not settings.accessibility_advisory_enabled:
        return {}
    try:
        from apps.worker.stages.accessibility import normalize_accessibility_facts, read_axe_version

        per_page = [
            {"url": page.final_url or page.url, "result": page.axe_results}
            for page in crawl_result.pages
            if getattr(page, "axe_results", None)
        ]
        if not per_page:
            return {}
        return normalize_accessibility_facts(
            per_page,
            max_examples=settings.accessibility_max_examples_per_issue,
            axe_version=read_axe_version(settings.accessibility_axe_script_path),
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return {}


def _mark_job(
    db: Session,
    job: AuditJob,
    status: AuditStatus,
    stage: str,
    progress_pct: int,
    error_message: str | None = None,
) -> None:
    now = datetime.now(UTC)
    if job.started_at is None and status != AuditStatus.QUEUED:
        job.started_at = now
    if status in {AuditStatus.COMPLETE, AuditStatus.FAILED}:
        job.completed_at = now
    job.status = status.value
    job.current_stage = stage
    job.progress_pct = progress_pct
    job.error_message = error_message
    db.commit()
    db.refresh(job)


def _psi_page_urls(crawl_result: CrawlResult, fallback_url: str) -> list[str]:
    urls = [page.final_url or page.url for page in crawl_result.pages]
    urls = urls or [crawl_result.final_url or fallback_url]
    # Second guard (the crawler already refuses them): a bot check's own page is never sent to
    # PageSpeed or the link sweep.
    return [url for url in urls if not is_interstitial_url(url)] or [fallback_url]


def _upsert_audit_result(
    db: Session,
    job: AuditJob,
    crawl_result: CrawlResult,
    psi_facts: JsonDict,
    seo_facts: JsonDict,
    uxui_facts: JsonDict,
    external_seo_facts: JsonDict,
    score_breakdown: JsonDict,
    commentary: JsonDict,
    validation_log: JsonDict,
    accessibility_facts: JsonDict | None = None,
) -> AuditResult:
    result = job.result
    if result is None:
        result = AuditResult(
            job_id=job.id,
            seo_score=score_breakdown["scores"]["seo"],
            uxui_score=score_breakdown["scores"]["uxui"],
            lead_gen_score=score_breakdown["scores"]["lead_gen"],
            crawled_pages={},
            seo_facts={},
            uxui_facts={},
            psi_facts={},
            external_seo_facts={},
            score_breakdown={},
            commentary={},
            validation_log={},
            report_metadata={},
            pdf_path=None,
            rubric_version=score_breakdown["rubric_version"],
            llm_model=str(commentary.get("model") or "not_configured"),
        )
        db.add(result)

    result.seo_score = score_breakdown["scores"]["seo"]
    result.uxui_score = score_breakdown["scores"]["uxui"]
    result.lead_gen_score = score_breakdown["scores"]["lead_gen"]
    result.crawled_pages = crawl_result.to_dict()
    result.seo_facts = seo_facts
    result.uxui_facts = uxui_facts
    result.psi_facts = psi_facts
    result.external_seo_facts = external_seo_facts
    # Advisory-only; stored separately from the scored facts. Empty => NULL => no report section.
    result.accessibility_facts = accessibility_facts or None
    result.score_breakdown = score_breakdown
    result.commentary = commentary
    result.validation_log = validation_log
    result.report_metadata = {
        "status": "pending_pdf_render",
        "renderer": "weasyprint",
        "collection_completed_at": datetime.now(UTC).isoformat(),
        "scoring_completed": True,
        "commentary_provider": commentary.get("provider"),
    }
    result.pdf_path = None
    result.rubric_version = score_breakdown["rubric_version"]
    result.llm_model = str(commentary.get("model") or "not_configured")
    db.commit()
    db.refresh(result)
    db.refresh(job)
    return result


def _store_export_results(
    db: Session,
    result: AuditResult,
    pdf_result: PdfRenderResult,
    docx_result: DocxRenderResult | None,
    docx_error: str | None = None,
) -> None:
    docx_metadata: JsonDict
    if docx_result is not None:
        docx_metadata = docx_result.report_metadata
    else:
        docx_metadata = {"status": "failed", "error": docx_error or "docx render failed"}
    result.report_metadata = {
        **pdf_result.report_metadata,
        "exports": {
            "pdf": pdf_result.report_metadata,
            "docx": docx_metadata,
        },
        "docx_path": docx_result.docx_path if docx_result else None,
        "docx_size_bytes": docx_result.size_bytes if docx_result else 0,
    }
    result.pdf_path = pdf_result.pdf_path
    db.commit()
    db.refresh(result)


def _render_docx_safely(job: AuditJob, result: AuditResult, settings: Settings):
    """The PDF is the primary deliverable; a DOCX failure must not fail the audit."""
    try:
        return render_audit_docx(job, result, settings), None
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return None, " ".join(str(exc).split())[:500]


def _page_urls_from_crawled_pages(crawled_pages: JsonDict, fallback_url: str) -> list[str]:
    pages = crawled_pages.get("pages")
    if not isinstance(pages, list):
        return [fallback_url]

    urls = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = page.get("final_url") or page.get("url")
        if url:
            urls.append(str(url))
    return urls or [fallback_url]


def _upsert_social_result(
    db: Session,
    job: AuditJob,
    social_facts: JsonDict,
    social_result: JsonDict,
) -> AuditResult:
    rubric_version = str(social_result.get("rubric_version") or "phase2-social")
    result = job.result
    if result is None:
        result = AuditResult(
            job_id=job.id,
            seo_score=None,
            uxui_score=None,
            lead_gen_score=None,
            crawled_pages={},
            seo_facts={},
            uxui_facts={},
            psi_facts={},
            external_seo_facts={},
            score_breakdown={},
            commentary={},
            validation_log={},
            report_metadata={},
            pdf_path=None,
            rubric_version=rubric_version,
            llm_model="deterministic",
        )
        db.add(result)

    result.seo_score = None
    result.uxui_score = None
    result.lead_gen_score = None
    result.social_score = social_result.get("score")
    result.social_facts = social_facts
    result.score_breakdown = social_result
    result.report_metadata = {
        "status": "pending_pdf_render",
        "renderer": "weasyprint",
        "report_kind": "social",
    }
    result.pdf_path = None
    result.rubric_version = rubric_version
    result.llm_model = "deterministic"
    db.commit()
    db.refresh(result)
    db.refresh(job)
    return result


def _augment_with_connected_youtube(
    social_facts: JsonDict,
    settings: Settings,
    db: Session,
    handles: dict[str, str] | None,
) -> None:
    """SAE-15 connected-mode YouTube (owner consent): attach the connected channel's Analytics
    metrics to the social facts — SMWA-140's pipeline wiring over the already-built OAuth-scope
    seam (``oauth_scopes`` adds the YT scopes to the Google consent when the flag is on).

    Every gate must pass: the ``youtube_analytics_connect_enabled`` flag (default OFF — prod
    unchanged), a YouTube handle on the audit, a Google connection whose GRANT actually includes
    the YouTube Analytics scopes (a pre-flag consent doesn't), and a working token. Presentation
    only — nothing here is scored — and best-effort like Places/PSI: any miss or failure leaves
    the facts untouched. Only SoftTimeLimitExceeded propagates."""
    if not getattr(settings, "youtube_analytics_connect_enabled", False):
        return
    youtube_handle = (handles or {}).get("youtube")
    if not youtube_handle:
        return
    try:
        connection = latest_google_connection(db)
        if connection is None:
            return
        granted = set((connection.scopes or {}).get("values") or [])
        if not all(scope in granted for scope in YOUTUBE_ANALYTICS_SCOPES):
            return
        access_token = ensure_google_access_token(connection, settings, db)
        # Identity gate (mirrors the Places website_mismatch gate): the Analytics API only
        # reports on the CONNECTED account's channel (ids=channel==MINE), so its private
        # metrics attach only when that channel IS the audited handle's — otherwise the
        # operator's own channel (connected once for GSC) would render as the client's.
        channel = fetch_own_channel(access_token, settings)
        if channel is None or not connected_channel_matches(youtube_handle, channel):
            return
        # Analytics data lags ~48-72h (same allowance as the GSC window); 90 days of it.
        # UTC like every other timestamp in the pipeline — a local-timezone "today" would
        # shrink the lag allowance on a worker east of UTC.
        end_date = datetime.now(UTC).date() - timedelta(days=3)
        start_date = end_date - timedelta(days=89)
        reports = fetch_channel_analytics(
            access_token,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            settings=settings,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        # ensure_google_access_token commits the refreshed token on THIS session: a failed
        # commit must not leave a PendingRollback session that would sink the caller's next
        # commit (same guard as _augment_with_benchmark_safely).
        db.rollback()
        return
    if reports is None:
        return
    social_facts["youtube_analytics"] = {
        "status": "complete",
        # The verified channel identity — rendered in the block's meta line so the reader can
        # see whose analytics these are.
        "channel": {
            "id": channel.get("id") or None,
            "handle": channel.get("custom_url") or None,
            "title": channel.get("title") or None,
        },
        "window": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        **normalize_youtube_analytics(reports),
    }


def _run_social_pipeline(
    db: Session,
    job: AuditJob,
    settings: Settings,
    social_collector: SocialCollectorFunc,
) -> None:
    if not settings.social_audits_enabled:
        # A social-only job queued before social audits were switched off: there is nothing to
        # audit without them, so it fails plainly before any provider is called.
        _mark_job(
            db,
            job,
            AuditStatus.FAILED,
            "Social media audits are off",
            job.progress_pct or 0,
            SOCIAL_AUDITS_DISABLED_MESSAGE,
        )
        return
    _mark_job(db, job, AuditStatus.CRAWLING, "Collecting social profiles", 40)
    social_facts = social_collector(settings, job.social_handles)
    if social_facts.get("status") in {"complete", "partial"}:
        _augment_with_connected_youtube(social_facts, settings, db, job.social_handles)

    _mark_job(db, job, AuditStatus.SCORING, "Scoring social profiles", 80)
    social_result = score_social_audit(social_facts, settings)

    result = _upsert_social_result(db, job, social_facts, social_result)

    _mark_job(db, job, AuditStatus.COMMENTING, "Writing social commentary", 88)
    # The FULL data (remediation included) whatever the report profile: the commentary step
    # needs the fixes as input, and stored data must not depend on how reports are rendered.
    baseline = compose_social_report_data(job, result)
    commentary = generate_social_commentary(
        audit_context={"handles": job.social_handles, "niche": job.niche},
        social_facts=social_facts,
        score=baseline.get("score"),
        findings=baseline.get("findings") or [],
        settings=settings,
    )
    result.commentary = commentary
    result.llm_model = commentary.get("model") or result.llm_model
    db.commit()
    db.refresh(result)

    _mark_job(db, job, AuditStatus.RENDERING, "Rendering social report", 95)
    pdf_result = render_social_pdf(job, result, settings)
    result.report_metadata = {
        **pdf_result.report_metadata,
        "exports": {"pdf": pdf_result.report_metadata},
    }
    result.pdf_path = pdf_result.pdf_path
    db.commit()
    db.refresh(result)

    _mark_job(db, job, AuditStatus.COMPLETE, "Social audit complete", 100)


def _explicit_social_handles(job: AuditJob) -> dict[str, str]:
    """The operator's typed, non-empty social handles (blanks dropped). Stored JSON is untrusted:
    a malformed non-dict value yields ``{}`` rather than raising, so the safety wrappers that fall
    back to this helper can never re-raise from it."""
    handles = job.social_handles
    if not isinstance(handles, dict):
        return {}
    return {k: v for k, v in handles.items() if v}


def _has_usable_social_credential(handles: dict[str, str], settings: Settings) -> bool:
    """True when at least one of ``handles``' platforms has a configured provider credential, so a
    social collection can actually return data (an Apify token / YouTube key)."""
    return any(
        (provider := get_provider(platform)) is not None and provider.credential_available(settings)
        for platform in handles
    )


def _resolve_social_handles(
    job: AuditJob,
    crawl_result: CrawlResult,
    settings: Settings,
) -> dict[str, str]:
    """Effective social handles for the social step: the operator's explicit handles, with any
    platform they left blank auto-filled from social profile links found on the crawled site (when
    ``social_autodiscovery_enabled``) — the per-platform blank-fill the new-audit form promises.
    Explicit handles always win per platform, so a typed Instagram link is kept verbatim while
    Facebook/YouTube get discovered from the page. ``site_url`` lets discovery prefer handles
    matching the audited brand. Pure over already-crawled HTML — no extra network call, so audits
    stay reproducible."""
    explicit = _explicit_social_handles(job)
    if not settings.social_autodiscovery_enabled:
        return explicit
    site_url = getattr(crawl_result, "final_url", None) or job.url
    discovered = discover_social_links(crawl_result.pages, site_url=site_url)
    # Back-fill only platforms whose provider can actually fetch: a discovered handle with no
    # credential would fail on every run (and every rerun), permanently pinning the social bundle
    # at "partial". Explicit operator handles are kept regardless — the operator asked, and the
    # report's collection-failure note is the honest outcome.
    usable_discovered = {
        platform: url
        for platform, url in discovered.items()
        if (provider := get_provider(platform)) is not None
        and provider.credential_available(settings)
    }
    # Discovery fills only the gaps; explicit entries override per platform.
    return {**usable_discovered, **explicit}


def _resolve_social_handles_safely(
    job: AuditJob,
    crawl_result: CrawlResult,
    settings: Settings,
) -> dict[str, str]:
    """Auto-discovery is a best-effort add-on that runs AFTER the website result is committed, so —
    like every other optional stage in this file (``_collect_external_seo_safely``,
    ``_render_docx_safely``) — it must never sink an already-scored website audit. Any failure
    resolving handles (e.g. an unexpected error parsing a page for social links) degrades to the
    operator's explicit handles. Only ``SoftTimeLimitExceeded`` propagates (task is out of time)."""
    try:
        return _resolve_social_handles(job, crawl_result, settings)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return _explicit_social_handles(job)


def _website_phone_keys(result: AuditResult) -> set[str]:
    """Comparable phone keys extracted from the website's UX/UI contact facts (extractor_uxui)."""
    keys: set[str] = set()
    uxui = result.uxui_facts if isinstance(result.uxui_facts, dict) else {}
    for page in uxui.get("pages") or []:
        contact = page.get("contact") if isinstance(page, dict) else None
        if not isinstance(contact, dict):
            continue
        for num in contact.get("phone_numbers") or []:
            if key := _phone_tail(num):
                keys.add(key)
    return keys


def _inject_nap_consistency(social_facts: JsonDict, result: AuditResult) -> None:
    """Combined-audit only (SAE-10/13): thin worker seam — reads the website's phone keys off
    the stored result, then delegates to the pure, schema-validated
    ``social.extractor.inject_nap_consistency`` (see its docstring for the semantics)."""
    inject_nap_consistency(social_facts, website_phone_keys=_website_phone_keys(result))


#  A brand token this short names no business — the same floor the GSC branded split applies
#  (``_branded_split``: ``len(token) < 3 -> {}``). Below it, no Places lookup is billed at all.
_MIN_BUSINESS_QUERY_CHARS = 3


def _business_query(job: AuditJob) -> str:
    """A Google Places text query for the audited business — the shared registrable brand
    label (``www.builderleadconverter.com`` -> ``builderleadconverter``, ``shop.acme.com`` ->
    ``acme``, ``smith.co.uk`` -> ``smith``, ``smithbuilders.wixsite.com`` ->
    ``smithbuilders``, ``sites.google.com/view/smith`` -> ``smith``), the same deriver the GSC
    branded split uses. A too-short/underivable label yields "" (the collector then skips —
    no billed Text Search for a junk query), and a match that IS made is still verified against
    the listing's website (``website_mismatch``), so a weak query yields no Google data rather
    than a stranger's listing."""
    label = registrable_brand_label(str(job.url or ""))
    return label if len(label) >= _MIN_BUSINESS_QUERY_CHARS else ""


def _augment_with_google_business(
    social_facts: JsonDict, settings: Settings, *, query: str, expected_url: str
) -> None:
    """Combined-audit only (SAE-13): enrich the social facts with the business's PUBLIC Google
    listing (Places API) — stashed under ``google_business`` for the report, plus the scored
    ``google_rating`` / ``google_review_count`` summary signals and a phone for the NAP check.
    The listing is accepted only when its website belongs to the audited site (see
    ``collect_google_business_facts``), so a fuzzy Text Search can't attribute a stranger's
    reviews/phone to the client. Graceful: no key / no match / any failure leaves the social
    facts untouched (the reviews rule then ``skip_if_missing``-rescales and the report shows no
    Google block)."""
    gbp = collect_google_business_facts(settings, query=query, expected_url=expected_url)
    if gbp.get("status") != "complete":
        return
    inject_google_business(social_facts, gbp.get("business") or {})


def _augment_with_social(
    db: Session,
    job: AuditJob,
    result: AuditResult,
    settings: Settings,
    social_collector: SocialCollectorFunc,
    handles: dict[str, str],
    *,
    promote: bool,
) -> None:
    """After the (untouched) website pipeline has scored + persisted, run the social audit over
    the resolved ``handles``, merge it onto the SAME result, and compute the Overall Lead-Gen
    Readiness score. Nothing is mutated or committed before the collection outcome is known:

    - ``promote=True`` (a plain website submission whose handles came from auto-discovery): the
      job is promoted to ``combined`` — handles written back, ``audit_type`` flipped, sections
      merged — ONLY when the collection produced usable data (status complete/partial with a
      score). Otherwise the website audit stays byte-identical: no handle write, no type flip,
      no breakdown keys.
    - ``promote=False`` (the operator explicitly asked for a combined audit): whatever the
      collection returned is merged — including a failed-status social section, which is
      informative to the operator who asked for social.

    The website columns and existing score_breakdown keys are left intact — we only ADD
    social_score, social_facts, and the ``social`` / ``overall_readiness`` keys; the existing
    RENDERING stage then produces ONE combined PDF (compose_report_payload appends the sections
    when it sees this data). Social findings here are deterministic (no LLM). Handles are passed
    in (not re-read off ``job``) so the data flow doesn't depend on a DB refresh landing first.

    A no-op while ``social_audits_enabled`` is off (the caller skips it too): nothing is
    collected, written or marked."""
    if not settings.social_audits_enabled:
        return
    _mark_job(db, job, AuditStatus.RENDERING, "Auditing social profiles", 96)
    # Graceful degradation: the social add-on must never sink an already-scored, already-committed
    # website audit. ALL the fallible work — network collect, rubric load, scoring, the merge
    # commit itself — happens inside this try, so a failure anywhere just leaves the website
    # result intact and the report renders website-only (no social/overall sections). The
    # rollback matters: a failed commit must not leave a PendingRollback session that would sink
    # the next _mark_job commit. Only SoftTimeLimitExceeded propagates (the task is out of time).
    try:
        social_facts = social_collector(settings, handles)
        collection_usable = social_facts.get("status") in {"complete", "partial"}
        if promote and not collection_usable:
            # Auto-discovery path with nothing usable: skip the billed Places enrichment +
            # scoring — the website audit stays byte-identical. Under today's scorer this gate
            # is equivalent to the `usable` check below (score is None exactly when status
            # isn't complete/partial); that later check stays as the invariant's backstop —
            # a job must never be promoted to combined without an actual Social Score, even
            # if a future scorer change makes score None on a complete collection.
            return
        if collection_usable:
            # Combined-only enrichment: the business's public Google listing (reviews/rating +
            # an authoritative phone), then the website<->business phone (NAP) cross-check.
            # Gated on a usable collection for BOTH paths: an explicitly-combined audit whose
            # collection FAILED merges the honest failure note alone — no billed Places lookup,
            # and no google_business block sitting in failed-status facts for a render surface
            # to leak while the PDF/DOCX suppress the social body.
            _augment_with_google_business(
                social_facts, settings, query=_business_query(job), expected_url=str(job.url or "")
            )
            inject_category_relevance(social_facts, niche=job.niche)
            _inject_nap_consistency(social_facts, result)
            _augment_with_connected_youtube(social_facts, settings, db, handles)
        social_result = score_social_audit(social_facts, settings)
        usable = collection_usable and social_result.get("score") is not None
        if promote and not usable:
            return
        overall = compose_overall_readiness_score(
            website_lead_gen=result.lead_gen_score,
            social_score=social_result.get("score"),
            settings=settings,
        )

        job.social_handles = dict(handles)
        if promote:
            job.audit_type = "combined"
        breakdown = dict(result.score_breakdown or {})
        breakdown["social"] = social_result
        breakdown["overall_readiness"] = overall
        result.social_score = social_result.get("score")
        result.social_facts = social_facts
        result.score_breakdown = breakdown
        db.commit()
        db.refresh(result)
        db.refresh(job)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        db.rollback()
        return


def _augment_with_benchmark_safely(
    db: Session,
    job: AuditJob,
    result: AuditResult,
    settings: Settings,
) -> None:
    """Enrichment (P2-26 / SMWA-79 — deferred v3): after the website/social result is committed,
    optionally present the audited scores relative to competitor / industry baselines by stashing
    normalized benchmark facts in ``score_breakdown["benchmark"]`` (no new DB column — same trick
    as ``overall_readiness``); ``compose_report_payload`` then appends the section. Benchmarking is
    presentation only — it NEVER changes a score.

    Best-effort and graceful like every other optional stage here: OFF by default
    (``benchmark_enabled``), and the collector skips at every not-ready state (no vendor / no key /
    the deferred stub no-op). A skip — or any failure — leaves the committed result intact, so the
    report renders without a benchmark section. Only ``SoftTimeLimitExceeded`` propagates."""
    if not settings.benchmark_enabled:
        return
    try:
        facts = collect_benchmark_facts(settings, target_url=job.url, niche=job.niche)
        if facts.get("status") not in {"complete", "partial"}:
            return
        breakdown = dict(result.score_breakdown or {})
        breakdown["benchmark"] = facts
        result.score_breakdown = breakdown
        db.commit()
        db.refresh(result)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        # Roll back so a failed commit doesn't leave the session in a poisoned
        # (PendingRollback) state that would sink the next _mark_job commit and flip the
        # already-committed website audit to FAILED. Leaves the website result intact.
        db.rollback()
        return


def _augment_with_ai_visibility_safely(
    db: Session,
    job: AuditJob,
    result: AuditResult,
    settings: Settings,
) -> None:
    """Enrichment: after the website/combined result is committed, fetch the Semrush AI Visibility
    data for the audited domain and stash it in ``score_breakdown["ai_visibility"]`` (no new DB
    column — same trick as ``benchmark`` / ``overall_readiness``); ``compose_report_payload`` then
    appends the AI Visibility section. Presentation only — it NEVER changes a score.

    Auto-runs on every website/combined audit when ``ai_visibility_enabled`` is set (OFF by
    default). Best-effort and graceful like every other optional stage: the collector skips at every
    not-ready state (no credentials / login blocked / fetch failed / disabled), and a skip — or ANY
    failure — leaves the committed result intact, so the report simply renders without an
    AI-visibility section. Only ``SoftTimeLimitExceeded`` propagates. Runs a real Semrush login-bot
    + vision extraction, so it adds latency; the dedicated ``rerun_ai_visibility`` task can refresh
    it later without re-running the whole audit."""
    if not settings.ai_visibility_enabled:
        return
    try:
        # Progress bump lives INSIDE the guard: _mark_job commits, and a commit error here must
        # degrade like any other AI-visibility failure — never escape to sink the committed audit.
        _mark_job(db, job, AuditStatus.RENDERING, "Collecting AI visibility insights", 97)
        domain = _domain_for_ai_visibility(job.url)
        facts = collect_ai_visibility_facts(
            settings, domain=domain, retrieved_at=datetime.now(UTC).isoformat()
        )
        # Store data (complete/partial) AND an honest block note (failed) so a CAPTCHA/login wall is
        # shown in the report, not silently omitted. Config skips (disabled/no creds) stay unstored.
        if facts.get("status") not in {"complete", "partial", "failed", "empty"}:
            return
        breakdown = dict(result.score_breakdown or {})
        breakdown["ai_visibility"] = facts
        result.score_breakdown = breakdown
        result.report_metadata = {
            **(result.report_metadata or {}),
            "ai_visibility_retrieved_at": facts.get("retrieved_at"),
            "ai_visibility_provider": facts.get("provider"),
        }
        db.commit()
        db.refresh(result)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        # Same poisoned-session guard as the benchmark stage: roll back so a failed AI-visibility
        # commit can't sink the already-committed audit. Leaves the result intact.
        db.rollback()
        return


def run_collection_audit(
    job_id: str,
    *,
    crawler: CrawlerFunc = crawl_site_sync,
    psi_collector: PsiCollectorFunc = collect_pagespeed_facts,
    social_collector: SocialCollectorFunc = collect_social_facts,
) -> None:
    settings = get_settings()
    task_started = time.monotonic()
    parsed_job_id = UUID(job_id)

    with SessionLocal() as db:
        job = db.get(AuditJob, parsed_job_id)
        if job is None:
            return

        # Idempotency: a redelivered task (worker crash + acks_late re-queue) must not re-run and
        # overwrite an audit that already finished. Re-running a queued/in-progress job is fine —
        # that is the crash recovery acks_late buys us.
        if (job.status or "") == AuditStatus.COMPLETE.value:
            return

        try:
            if (job.audit_type or "website") == "social":
                _run_social_pipeline(db, job, settings, social_collector)
                return

            _mark_job(db, job, AuditStatus.CRAWLING, "Rendering website pages", 15)
            crawl_result = crawler(job.url, settings, str(job.id))
            if is_interstitial_url(crawl_result.final_url):
                # Second guard: the crawler never returns a bot check's page as the homepage. If
                # one slips through, fail plainly rather than score it or send it to PageSpeed.
                raise SiteBlockedError(BOT_CHECK_BLOCKED_MESSAGE)

            _mark_job(
                db,
                job,
                AuditStatus.COLLECTING_PERFORMANCE,
                "Collecting PageSpeed Insights",
                45,
            )
            psi_facts = psi_collector(_psi_page_urls(crawl_result, job.url), settings)

            _mark_job(db, job, AuditStatus.EXTRACTING, "Extracting SEO and UX/UI facts", 70)
            seo_facts = extract_seo_facts(crawl_result.pages)
            uxui_facts = extract_uxui_facts(crawl_result.pages)
            # Advisory-only; computed from axe results gathered during the crawl. Deliberately
            # NOT passed to score_audit / generate_commentary below, so scores are identical
            # whether this opt-in pass ran or not.
            accessibility_facts = _collect_accessibility_advisory_safely(crawl_result, settings)

            _mark_job(db, job, AuditStatus.EXTRACTING, "Collecting external SEO insights", 76)
            external_seo_facts = _collect_external_seo_safely(
                url=job.url,
                audit_id=str(job.id),
                page_urls=_psi_page_urls(crawl_result, job.url),
                settings=settings,
                db=db,
                seo_facts=seo_facts,
                crawled_pages=crawl_result.to_dict(),
                rendered_pages=crawl_result.pages,
                deadline=_external_enrichment_deadline(settings, task_started),
            )

            _mark_job(db, job, AuditStatus.SCORING, "Scoring extracted facts", 80)
            score_breakdown = score_audit(
                seo_facts,
                uxui_facts,
                psi_facts,
                settings,
                external_seo_facts=external_seo_facts,
            )

            _mark_job(db, job, AuditStatus.COMMENTING, "Generating grounded commentary", 88)
            commentary = generate_commentary(
                audit_context={
                    "url": job.url,
                    "niche": job.niche,
                    "target_audience": job.target_audience,
                },
                seo_facts=seo_facts,
                uxui_facts=uxui_facts,
                psi_facts=psi_facts,
                external_seo_facts=external_seo_facts,
                score_breakdown=score_breakdown,
                settings=settings,
            )

            _mark_job(db, job, AuditStatus.VALIDATING, "Validating commentary grounding", 95)
            commentary, validation_log = validate_commentary_grounding(
                commentary,
                fact_sources={
                    "seo_facts": seo_facts,
                    "uxui_facts": uxui_facts,
                    "psi_facts": psi_facts,
                    "external_seo_facts": external_seo_facts,
                    # Only the headline scores are citable numbers. The full score
                    # breakdown's rule weights, params, and ratios are internal scoring
                    # mechanics and must not count as "grounding" for LLM numeric claims.
                    "scores": score_breakdown.get("scores", {}),
                },
            )

            result = _upsert_audit_result(
                db,
                job,
                crawl_result,
                psi_facts,
                seo_facts,
                uxui_facts,
                external_seo_facts,
                score_breakdown,
                commentary,
                validation_log,
                accessibility_facts=accessibility_facts,
            )

            # Social: the operator's handles, plus (for a plain website submission) any platform
            # auto-filled from social profile links found on the crawled site. The social step
            # runs only when it could produce data — i.e. a provider credential is configured for
            # a resolved platform; an explicit combined audit always runs (and degrades
            # gracefully if its key is absent). Promotion of a website audit to combined happens
            # INSIDE _augment_with_social, only after the collection actually returned usable
            # data — a website audit that links to no profiles, has no provider credential, or
            # whose collection comes back empty stays byte-identical to before.
            # With social audits switched off there is no social step for any job: no link
            # discovery and no collection, even for a combined job queued before the switch with
            # typed handles (they stay stored, unused), so the report stays website-only.
            if not settings.social_audits_enabled and (
                result.social_score is not None or result.social_facts
            ):
                # A task redelivered after its worker was lost across the deploy that switched
                # social audits off can find the social data its first attempt merged (at 96%)
                # before dying; the website upsert above does not touch it. Drop it, or the
                # report would still show a social section. An audit completed before the switch
                # never reaches this line (the COMPLETE return above) and keeps its own.
                result.social_score = None
                result.social_facts = None
                db.commit()
                db.refresh(result)
            effective_handles = (
                _resolve_social_handles_safely(job, crawl_result, settings)
                if settings.social_audits_enabled
                else {}
            )
            already_combined = (job.audit_type or "website") == "combined"
            if effective_handles and (
                already_combined or _has_usable_social_credential(effective_handles, settings)
            ):
                _augment_with_social(
                    db,
                    job,
                    result,
                    settings,
                    social_collector,
                    effective_handles,
                    promote=not already_combined,
                )

            # Enrichment (deferred v3): optionally present the scores vs competitor/industry
            # baselines. No-op unless benchmark_enabled; never sinks the committed result.
            _augment_with_benchmark_safely(db, job, result, settings)

            # Enrichment: Semrush AI Visibility. Auto-runs on every website/combined audit when
            # enabled — a Semrush login-bot + vision extraction fills the AI Visibility section.
            # No-op unless ai_visibility_enabled; a skip/failure never sinks the committed result.
            _augment_with_ai_visibility_safely(db, job, result, settings)

            _mark_job(db, job, AuditStatus.RENDERING, "Rendering report exports", 98)
            pdf_result = render_audit_pdf(job, result, settings)
            docx_result, docx_error = _render_docx_safely(job, result, settings)
            _store_export_results(db, result, pdf_result, docx_result, docx_error)

            _mark_job(db, job, AuditStatus.COMPLETE, "Audit report complete", 100)
        except SoftTimeLimitExceeded:
            # The soft time limit fired: mark the job FAILED with an honest reason (not an empty
            # exception repr), then re-raise so Celery records + acks it (no redelivery loop).
            db.rollback()
            timed_out = db.get(AuditJob, parsed_job_id)
            if timed_out is not None:
                _mark_job(
                    db,
                    timed_out,
                    AuditStatus.FAILED,
                    "Audit timed out",
                    timed_out.progress_pct or 0,
                    "Audit exceeded the time limit and was stopped before completing.",
                )
            raise
        except Exception as exc:
            db.rollback()
            failed_job = db.get(AuditJob, parsed_job_id)
            if failed_job is not None:
                _mark_job(
                    db,
                    failed_job,
                    AuditStatus.FAILED,
                    "Audit collection failed",
                    failed_job.progress_pct or 0,
                    str(exc),
                )
            raise


@celery_app.task(name="apps.worker.tasks.run_audit")
def run_audit(job_id: str) -> None:
    run_collection_audit(job_id)


def rerun_external_enrichment_for_audit(job_id: str) -> None:
    settings = get_settings()
    task_started = time.monotonic()
    parsed_job_id = UUID(job_id)

    with SessionLocal() as db:
        job = db.get(AuditJob, parsed_job_id)
        if job is None or job.result is None:
            return

        # Snapshot the fields the rerun overwrites. The enriched values are
        # committed BEFORE the PDF re-renders; if rendering then fails, restoring
        # this snapshot keeps the stored scores consistent with the PDF the
        # operator can still download.
        previous = {
            "external_seo_facts": job.result.external_seo_facts,
            "score_breakdown": job.result.score_breakdown,
            "commentary": job.result.commentary,
            "validation_log": job.result.validation_log,
            "seo_score": job.result.seo_score,
            "uxui_score": job.result.uxui_score,
            "lead_gen_score": job.result.lead_gen_score,
            "rubric_version": job.result.rubric_version,
            "llm_model": job.result.llm_model,
            "report_metadata": job.result.report_metadata,
        }

        try:
            result = job.result
            _mark_job(db, job, AuditStatus.EXTRACTING, "Collecting external SEO insights", 76)
            crawled_pages = result.crawled_pages or {}
            page_urls = _page_urls_from_crawled_pages(crawled_pages, job.url)
            external_seo_facts = _collect_external_seo_safely(
                url=job.url,
                audit_id=str(job.id),
                page_urls=page_urls,
                settings=settings,
                db=db,
                seo_facts=result.seo_facts or {},
                crawled_pages=crawled_pages,
                rendered_pages=None,
                deadline=_external_enrichment_deadline(settings, task_started),
            )

            _mark_job(db, job, AuditStatus.SCORING, "Rescoring enriched audit facts", 82)
            seo_facts = result.seo_facts or {}
            uxui_facts = result.uxui_facts or {}
            psi_facts = result.psi_facts or {}
            score_breakdown = score_audit(
                seo_facts,
                uxui_facts,
                psi_facts,
                settings,
                external_seo_facts=external_seo_facts,
            )

            _mark_job(db, job, AuditStatus.COMMENTING, "Refreshing grounded commentary", 88)
            commentary = generate_commentary(
                audit_context={
                    "url": job.url,
                    "niche": job.niche,
                    "target_audience": job.target_audience,
                },
                seo_facts=seo_facts,
                uxui_facts=uxui_facts,
                psi_facts=psi_facts,
                external_seo_facts=external_seo_facts,
                score_breakdown=score_breakdown,
                settings=settings,
            )

            _mark_job(db, job, AuditStatus.VALIDATING, "Validating refreshed commentary", 95)
            commentary, validation_log = validate_commentary_grounding(
                commentary,
                fact_sources={
                    "seo_facts": seo_facts,
                    "uxui_facts": uxui_facts,
                    "psi_facts": psi_facts,
                    "external_seo_facts": external_seo_facts,
                    "scores": score_breakdown.get("scores", {}),
                },
            )

            # The rescore above is website-only (`score_audit` only emits scores/categories/
            # rubric_version/etc.), so it lost every enrichment add-on key the pipeline had stored
            # — `social`, `benchmark`, and any future one. Enrichment is NOT re-run here, so carry
            # forward every stored key the website rescore didn't produce, generically, instead of
            # naming each (a named re-attach silently drops the next add-on on rerun). Then
            # recompute only `overall_readiness` from the new lead-gen score + the stored Social
            # Score, so the combined headline reflects the rescore.
            prev_breakdown = previous["score_breakdown"]
            if isinstance(prev_breakdown, dict):
                for key, value in prev_breakdown.items():
                    score_breakdown.setdefault(key, value)
            # A combined-typed job whose social step never produced data must stay website-only
            # on rerun: only recompute the overall headline when the audit actually has social
            # data or the stored breakdown already carried the key.
            had_overall = "overall_readiness" in (
                prev_breakdown if isinstance(prev_breakdown, dict) else {}
            )
            if (job.audit_type or "website") == "combined" and (
                result.social_score is not None or had_overall
            ):
                score_breakdown["overall_readiness"] = compose_overall_readiness_score(
                    website_lead_gen=score_breakdown["scores"]["lead_gen"],
                    social_score=result.social_score,
                    settings=settings,
                )

            result.external_seo_facts = external_seo_facts
            result.score_breakdown = score_breakdown
            result.commentary = commentary
            result.validation_log = validation_log
            result.seo_score = score_breakdown["scores"]["seo"]
            result.uxui_score = score_breakdown["scores"]["uxui"]
            result.lead_gen_score = score_breakdown["scores"]["lead_gen"]
            result.rubric_version = score_breakdown["rubric_version"]
            result.llm_model = str(commentary.get("model") or "not_configured")
            result.report_metadata = {
                **(result.report_metadata or {}),
                "status": "pending_export_render",
                "collection_completed_at": datetime.now(UTC).isoformat(),
                "external_enrichment_completed": True,
                "commentary_provider": commentary.get("provider"),
            }
            db.commit()
            db.refresh(result)

            _mark_job(db, job, AuditStatus.RENDERING, "Rendering enriched report exports", 98)
            pdf_result = render_audit_pdf(job, result, settings)
            docx_result, docx_error = _render_docx_safely(job, result, settings)
            _store_export_results(db, result, pdf_result, docx_result, docx_error)

            _mark_job(db, job, AuditStatus.COMPLETE, "Audit report complete", 100)
        except Exception as exc:
            db.rollback()
            failed_job = db.get(AuditJob, parsed_job_id)
            if failed_job is not None and failed_job.result is not None:
                # The original audit result and report still exist — a failed
                # enrichment rerun must not flip a COMPLETE audit to FAILED.
                # Restore the snapshot so the stored scores/commentary match the
                # report the operator can still download (the mid-rerun commit may
                # have already persisted enriched values the PDF never rendered).
                for field, value in previous.items():
                    setattr(failed_job.result, field, value)
                _mark_job(
                    db,
                    failed_job,
                    AuditStatus.COMPLETE,
                    "External SEO enrichment failed; previous report kept",
                    100,
                    str(exc),
                )
            elif failed_job is not None:
                _mark_job(
                    db,
                    failed_job,
                    AuditStatus.FAILED,
                    "External SEO enrichment failed",
                    failed_job.progress_pct or 0,
                    str(exc),
                )
            raise


@celery_app.task(name="apps.worker.tasks.rerun_external_enrichment")
def rerun_external_enrichment(job_id: str) -> None:
    rerun_external_enrichment_for_audit(job_id)


def _domain_for_ai_visibility(url: str) -> str:
    """The bare registrable-ish host Semrush's AI Visibility search expects (drops scheme/www)."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or url or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def rerun_ai_visibility_for_audit(
    job_id: str,
    *,
    fetcher: AiVisibilityCollectorFunc = collect_ai_visibility_facts,
) -> None:
    """On-demand AI Visibility enrichment for an already-complete audit.

    Mirrors ``rerun_external_enrichment_for_audit``: snapshot the fields it overwrites, collect the
    Semrush AI-visibility facts (via the injectable ``fetcher``), stash them in
    ``score_breakdown["ai_visibility"]`` (no new column — same trick as ``benchmark``/
    ``overall_readiness``; the external-enrichment rerun carries the key forward generically), and
    re-render. A skip (disabled / no credentials / failed scrape) or ANY failure keeps the existing
    COMPLETE report — AI visibility is presentation only and never sinks the audit. On failure the
    snapshot is restored and the job is re-marked COMPLETE, THEN the exception is re-raised so
    Celery records the failure (same contract as ``rerun_external_enrichment_for_audit``); only the
    render of NEW facts is lost, never the prior report."""
    settings = get_settings()
    parsed_job_id = UUID(job_id)

    with SessionLocal() as db:
        job = db.get(AuditJob, parsed_job_id)
        if job is None or job.result is None:
            return

        # Snapshot the fields the rerun overwrites; restored on failure so the stored values keep
        # matching the report the operator can still download.
        previous = {
            "score_breakdown": job.result.score_breakdown,
            "report_metadata": job.result.report_metadata,
            "pdf_path": job.result.pdf_path,
        }

        try:
            result = job.result
            _mark_job(db, job, AuditStatus.EXTRACTING, "Collecting AI visibility insights", 80)
            domain = _domain_for_ai_visibility(job.url)
            facts = fetcher(
                settings,
                domain=domain,
                retrieved_at=datetime.now(UTC).isoformat(),
            )

            if facts.get("status") not in {"complete", "partial", "failed", "empty"}:
                # Only a config skip (disabled / no creds) reaches here — nothing to show, leave the
                # existing report untouched. A blocked run (status "failed") IS stored below so its
                # "could not retrieve" note renders.
                _mark_job(
                    db,
                    job,
                    AuditStatus.COMPLETE,
                    "AI visibility data unavailable; previous report kept",
                    100,
                )
                return

            breakdown = dict(result.score_breakdown or {})
            breakdown["ai_visibility"] = facts
            result.score_breakdown = breakdown
            result.report_metadata = {
                **(result.report_metadata or {}),
                "ai_visibility_retrieved_at": facts.get("retrieved_at"),
                "ai_visibility_provider": facts.get("provider"),
            }
            db.commit()
            db.refresh(result)

            _mark_job(db, job, AuditStatus.RENDERING, "Rendering report with AI visibility", 98)
            pdf_result = render_audit_pdf(job, result, settings)
            docx_result, docx_error = _render_docx_safely(job, result, settings)
            _store_export_results(db, result, pdf_result, docx_result, docx_error)

            _mark_job(db, job, AuditStatus.COMPLETE, "Audit report complete", 100)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            db.rollback()
            failed_job = db.get(AuditJob, parsed_job_id)
            if failed_job is not None and failed_job.result is not None:
                # A failed AI-visibility rerun must not flip a COMPLETE audit to FAILED: restore the
                # snapshot so the stored values match the report still on disk.
                for field, value in previous.items():
                    setattr(failed_job.result, field, value)
                _mark_job(
                    db,
                    failed_job,
                    AuditStatus.COMPLETE,
                    "AI visibility enrichment failed; previous report kept",
                    100,
                    str(exc),
                )
            raise


@celery_app.task(name="apps.worker.tasks.rerun_ai_visibility")
def rerun_ai_visibility(job_id: str) -> None:
    rerun_ai_visibility_for_audit(job_id)
