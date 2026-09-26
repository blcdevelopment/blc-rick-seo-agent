from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from apps.shared.config import Settings
from apps.worker.stages.grounding_validator import (
    collect_social_known_numbers,
    social_text_has_ungrounded,
)

JsonDict = dict[str, Any]


class CommentaryFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "low", "medium", "high"]
    title: str
    # Structured, card-friendly prose. ``meaning`` = "what it means" (+ the measurement),
    # ``why`` = "why it matters". ``explanation`` is the legacy single-paragraph form,
    # kept for the DOCX export and the grounding/LLM path; the PDF renders the structured
    # fields and falls back to ``explanation`` only when they are absent.
    meaning: str = ""
    why: str = ""
    explanation: str = ""
    # Where to start, rendered as a bulleted list (URLs / locations, not numeric claims).
    location_label: str = ""
    location_urls: list[str] = Field(default_factory=list)
    evidence_refs: list[str]
    # The fix, carried on the finding itself so the report presents ONE card per issue
    # (problem + "Do this") instead of a separate, repetitive Recommendations list.
    # Defaults keep previously stored commentary payloads valid.
    action_items: list[str] = Field(default_factory=list)
    tier: str = ""


class CommentaryRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["quick_win", "mid_term", "long_term"]
    title: str
    rationale: str
    action_items: list[str]
    location_label: str = ""
    location_urls: list[str] = Field(default_factory=list)


class CommentarySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str
    findings: list[CommentaryFinding]
    recommendations: list[CommentaryRecommendation]


class CommentaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executive_summary: str
    seo: CommentarySection
    uxui: CommentarySection
    lead_generation: CommentarySection


def generate_commentary(
    *,
    audit_context: JsonDict,
    seo_facts: JsonDict,
    uxui_facts: JsonDict,
    psi_facts: JsonDict,
    external_seo_facts: JsonDict | None = None,
    score_breakdown: JsonDict,
    settings: Settings,
) -> JsonDict:
    # Imported lazily: content_plan imports the commentary models defined in this module,
    # so a top-level import here would be circular.
    from apps.worker.stages.content_plan import build_content_plan

    plan = build_content_plan(
        audit_context=audit_context,
        seo_facts=seo_facts,
        uxui_facts=uxui_facts,
        psi_facts=psi_facts,
        external_seo_facts=external_seo_facts,
        score_breakdown=score_breakdown,
        settings=settings,
    )

    # Phase 1: the deterministic plan IS the report - identical findings, order, severity,
    # and tier on every run, with or without an OpenAI key. The optional LLM polish layer
    # (Phase 2) will rewrite the prose strings here when a key is configured, without
    # changing the structure. See docs/RUBRICS.md §8.
    return {
        "status": "deterministic",
        "provider": "deterministic",
        "model": "deterministic",
        "content": plan.model_dump(mode="json"),
    }


def validate_commentary_content(payload: JsonDict) -> CommentaryContent:
    return CommentaryContent.model_validate(payload)


def commentary_json_schema() -> JsonDict:
    return CommentaryContent.model_json_schema()


def _call_openai(
    *,
    api_key: str,
    audit_context: JsonDict,
    seo_facts: JsonDict,
    uxui_facts: JsonDict,
    psi_facts: JsonDict,
    score_breakdown: JsonDict,
    settings: Settings,
) -> CommentaryContent:
    # Retained for Phase 2 (LLM polish): structured-output plumbing that the polish layer
    # will reuse to rewrite the deterministic plan's prose. Not called in Phase 1.
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=settings.openai_timeout_seconds)
    system_prompt = _read_prompt(settings.commentary_system_prompt_path)
    user_prompt = _render_user_prompt(
        settings.commentary_user_prompt_path,
        audit_context=audit_context,
        facts={
            "seo": _compact_facts(seo_facts),
            "uxui": _compact_facts(uxui_facts),
            "psi": _compact_facts(psi_facts),
        },
        score_breakdown=score_breakdown,
    )
    response = client.responses.parse(
        model=settings.openai_model,
        instructions=system_prompt,
        input=user_prompt,
        text_format=CommentaryContent,
        max_output_tokens=settings.openai_max_tokens,
        temperature=settings.openai_temperature,
    )
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, CommentaryContent):
        return parsed

    raise ValueError("OpenAI response did not include parsed structured commentary output.")


def _render_user_prompt(
    path: Path,
    *,
    audit_context: JsonDict,
    facts: JsonDict,
    score_breakdown: JsonDict,
) -> str:
    template = Template(_read_prompt(path))
    score_summary = score_breakdown.get("scores", {})
    return template.safe_substitute(
        url=audit_context.get("url") or "not provided",
        niche=audit_context.get("niche") or "not provided",
        target_audience=audit_context.get("target_audience") or "not provided",
        score_summary_json=_to_json(score_summary),
        facts_json=_to_json(facts),
        score_breakdown_json=_to_json(_compact_score_breakdown(score_breakdown)),
    )


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _compact_facts(payload: JsonDict) -> JsonDict:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"html", "rendered_html", "text"} and key != "pages"
    } | {"pages": _compact_pages(payload.get("pages", []))}


def _compact_pages(pages: Any) -> list[JsonDict]:
    if not isinstance(pages, list):
        return []
    compacted: list[JsonDict] = []
    for page in pages[:10]:
        if not isinstance(page, dict):
            continue
        compacted.append(
            {
                key: value
                for key, value in page.items()
                if key not in {"html", "rendered_html", "text"}
            }
        )
    return compacted


def _compact_score_breakdown(score_breakdown: JsonDict) -> JsonDict:
    compacted = dict(score_breakdown)
    categories = compacted.get("categories", {})
    if isinstance(categories, dict):
        compacted["categories"] = {
            name: _compact_category_breakdown(category)
            for name, category in categories.items()
            if isinstance(category, dict)
        }
    return compacted


def _compact_category_breakdown(category: JsonDict) -> JsonDict:
    compacted = dict(category)
    rules = compacted.get("rules", [])
    if isinstance(rules, list):
        compacted["rules"] = [
            rule
            for rule in rules
            if isinstance(rule, dict) and rule.get("result") in {"fail", "partial", "skipped"}
        ]
    return compacted


def _to_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


# --- Social audit commentary: optional LLM polish over the deterministic, rule-derived ---
# --- findings. No key => deterministic baseline (identical to today). With a key, the LLM ---
# --- only REPHRASES the given findings; a grounding backstop replaces any narrative that ---
# --- introduces a fabricated percentage or large count with the vetted deterministic text. ---


class SocialCommentaryFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    narrative: str


class SocialCommentaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executive_summary: str
    findings: list[SocialCommentaryFinding]


def _social_band(score: int | None) -> str:
    if score is None:
        return "not scored"
    if score >= 75:
        return "strong"
    if score >= 50:
        return "fair"
    return "needs work"


def _deterministic_social_commentary(*, score: int | None, findings: list[JsonDict]) -> JsonDict:
    if score is None:
        summary = "Social data could not be fully collected, so a Social Score is not available."
    else:
        summary = f"This social presence scored {score}/100 ({_social_band(score)})."
        if findings:
            count = len(findings)
            noun = "opportunity" if count == 1 else "opportunities"
            summary += f" The audit flagged {count} {noun} to strengthen lead generation."
    return {
        "executive_summary": summary,
        "findings": [
            {
                "id": finding.get("id") or "",
                "title": finding.get("label") or finding.get("id") or "",
                "narrative": finding.get("remediation") or "",
            }
            for finding in findings
        ],
    }


def generate_social_commentary(
    *,
    audit_context: JsonDict,
    social_facts: JsonDict,
    score: int | None,
    findings: list[JsonDict],
    settings: Settings,
) -> JsonDict:
    """Return social commentary (executive summary + per-finding narrative).

    Deterministic baseline by default; if an OpenAI key is set, the LLM rephrases the
    rule-derived findings into client-ready prose, then a grounding backstop swaps any
    narrative that introduces a fabricated stat back to the vetted deterministic text.
    """
    baseline = _deterministic_social_commentary(score=score, findings=findings)
    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    if not api_key or not findings:
        return {
            "status": "deterministic",
            "provider": "deterministic",
            "model": "deterministic",
            "content": baseline,
        }
    try:
        content = _call_openai_social(
            api_key=api_key,
            audit_context=audit_context,
            social_facts=social_facts,
            score=score,
            findings=findings,
            settings=settings,
        )
    except Exception:
        # Any LLM/network failure => keep the deterministic report (never fail the audit).
        return {
            "status": "deterministic_fallback",
            "provider": "deterministic",
            "model": "deterministic",
            "content": baseline,
        }
    known = collect_social_known_numbers(social_facts, findings)
    return {
        "status": "llm",
        "provider": "openai",
        "model": settings.openai_model,
        "content": _ground_social_commentary(content, baseline, known),
    }


def _ground_social_commentary(
    content: SocialCommentaryContent, baseline: JsonDict, known: set[str]
) -> JsonDict:
    llm = {finding.id: finding for finding in content.findings}
    summary = content.executive_summary.strip()
    if not summary or social_text_has_ungrounded(summary, known):
        summary = baseline["executive_summary"]
    merged: list[JsonDict] = []
    for base in baseline["findings"]:
        polished = llm.get(base["id"])
        narrative = polished.narrative.strip() if polished else ""
        if not narrative or social_text_has_ungrounded(narrative, known):
            narrative = base["narrative"]
        title = polished.title.strip() if polished and polished.title.strip() else base["title"]
        merged.append({"id": base["id"], "title": title, "narrative": narrative})
    return {"executive_summary": summary, "findings": merged}


def _call_openai_social(
    *,
    api_key: str,
    audit_context: JsonDict,
    social_facts: JsonDict,
    score: int | None,
    findings: list[JsonDict],
    settings: Settings,
) -> SocialCommentaryContent:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=settings.openai_timeout_seconds)
    system_prompt = _read_prompt(settings.commentary_social_system_prompt_path)
    template = Template(_read_prompt(settings.commentary_social_user_prompt_path))
    user_prompt = template.safe_substitute(
        handles=_to_json(audit_context.get("handles") or {}),
        score="not scored" if score is None else str(score),
        status=social_facts.get("status") or "unknown",
        summary_json=_to_json(social_facts.get("summary") or {}),
        findings_json=_to_json(
            [
                {
                    "id": finding.get("id"),
                    "label": finding.get("label"),
                    "result": finding.get("result"),
                    "impact": finding.get("impact"),
                    "remediation": finding.get("remediation"),
                }
                for finding in findings
            ]
        ),
    )
    response = client.responses.parse(
        model=settings.openai_model,
        instructions=system_prompt,
        input=user_prompt,
        text_format=SocialCommentaryContent,
        max_output_tokens=settings.openai_max_tokens,
        temperature=settings.openai_temperature,
    )
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, SocialCommentaryContent):
        return parsed
    raise ValueError("OpenAI response did not include parsed structured social commentary.")
