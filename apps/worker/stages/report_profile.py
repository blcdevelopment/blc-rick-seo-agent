"""Report profiles: "full" (the complete report) and "teaser" (the Rick edition).

A teaser shows the problems (findings, severity, evidence, scores) and none of the fixes: no
"Do this" action items, no recommendations, no roadmap or tiers, no rubric remediation, no
technical "recommended fix", no axe fix guidance, and no social narrative (which either IS the
remediation or is an LLM rewrite asked to explain the fix). Surfaces render a booking
call-to-action in their place.

Both report composers call into this module as their LAST step (compose_report_payload for
website/combined audits, compose_social_report_payload for standalone social audits), so the
PDF, the DOCX, the API detail JSON, the public share link and the UI all read the same stripped
payload and can never disagree. Stored data is untouched: the rubric remediation stays in
score_breakdown and the commentary JSON, scores never change, and switching back to "full"
restores every fix on the next render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from apps.shared.config import Settings
from apps.worker.stages.commentary import deterministic_social_summary
from apps.worker.stages.content_plan import (
    EXECUTIVE_SUMMARY_CLOSER,
    HOMEPAGE_LOCATION_LABEL,
    LOCATION_LABEL,
)

if TYPE_CHECKING:
    from apps.worker.stages.report_payload import ReportPayload

JsonDict = dict[str, Any]
ReportProfile = Literal["full", "teaser"]

# Second sentence of report_payload's generic fallback summary: it points the reader at the
# roadmap, which a teaser does not have.
FALLBACK_SUMMARY_ADVICE = (
    "Use the prioritized roadmap and score breakdown to address the highest-confidence "
    "lead generation opportunities first."
)
# "Start by checking ..." tells the reader where to begin fixing; a teaser only says where the
# problem was found.
_TEASER_LOCATION_LABELS = {
    LOCATION_LABEL: "Where it was found",
    HOMEPAGE_LOCATION_LABEL: "Found on the homepage",
}
TEASER_CTA_MESSAGE = (
    "This audit shows what is holding the site back and why it matters. The step-by-step "
    "fixes, in priority order, are covered in a short one-to-one meeting."
)


class ReportCta(BaseModel):
    """The teaser's call-to-action, rendered where the full report prints its fixes."""

    model_config = ConfigDict(extra="forbid")

    label: str
    url: str = ""
    message: str


def booking_cta(settings: Settings) -> ReportCta:
    return ReportCta(
        label=settings.booking_cta_label,
        url=settings.booking_url,
        message=TEASER_CTA_MESSAGE,
    )


def strip_fix_advice(summary: str) -> str:
    """Drop the executive summary's generic how-to-proceed advice; every finding stays."""
    for sentence in (EXECUTIVE_SUMMARY_CLOSER, FALLBACK_SUMMARY_ADVICE):
        summary = summary.replace(sentence, "")
    return " ".join(summary.split())


def apply_report_profile(payload: ReportPayload, settings: Settings) -> ReportPayload:
    """The website/combined payload for the configured profile: unchanged for "full", fix-free
    for "teaser"."""
    if settings.report_profile != "teaser":
        return payload
    sections = [
        section.model_copy(
            update={
                "findings": [
                    finding.model_copy(
                        update={
                            "action_items": [],
                            "tier": "",
                            "location_label": _TEASER_LOCATION_LABELS.get(
                                finding.location_label, finding.location_label
                            ),
                        }
                    )
                    for finding in section.findings
                ],
                "recommendations": [],
                "show_recommendations": False,
            }
        )
        for section in payload.sections
    ]
    technical = payload.technical_seo_section
    accessibility = payload.accessibility_advisory_section
    return payload.model_copy(
        update={
            "executive_summary": strip_fix_advice(payload.executive_summary),
            "sections": sections,
            "roadmap": [],
            "technical_seo_section": technical.model_copy(
                update={
                    "issues": [
                        issue.model_copy(update={"recommended_fix": ""})
                        for issue in technical.issues
                    ]
                }
            ),
            "accessibility_advisory_section": accessibility.model_copy(
                update={
                    "issues": [
                        issue.model_copy(update={"help_url": "", "failure_summary": ""})
                        for issue in accessibility.issues
                    ]
                }
            ),
            "social_audit": (
                teaser_social_data(payload.social_audit) if payload.social_audit else None
            ),
            # An AI-visibility block that could not collect carries only an operator note
            # ("reconnect Semrush"), which has no place in a prospect-facing teaser.
            "ai_visibility": (
                payload.ai_visibility
                if payload.ai_visibility and not payload.ai_visibility.get("unavailable")
                else None
            ),
            "report_profile": "teaser",
            "cta": booking_cta(settings),
        }
    )


def teaser_social_data(data: JsonDict) -> JsonDict:
    """The social report with every fix removed. Each finding keeps its label, metric and impact
    but loses its remediation, tier and narrative; the tiered roadmap (copies of the same
    findings) is dropped; an LLM-written executive summary gives way to the rule-derived one."""
    findings = [
        {**finding, "remediation": None, "tier": None, "narrative": ""}
        for finding in data.get("findings") or []
        if isinstance(finding, dict)
    ]
    summary = str(data.get("executive_summary") or "")
    if summary:
        summary = deterministic_social_summary(data.get("score"), len(findings))
    return {
        **data,
        "findings": findings,
        "roadmap": {},
        "executive_summary": summary,
        "commentary_provider": "deterministic",
    }


def apply_social_report_profile(data: JsonDict, settings: Settings) -> JsonDict:
    """The standalone social report for the configured profile, tagged so its template and the
    UI know which one they render."""
    if settings.report_profile != "teaser":
        return {**data, "report_profile": "full", "cta": None}
    return {
        **teaser_social_data(data),
        "report_profile": "teaser",
        "cta": booking_cta(settings).model_dump(),
    }
