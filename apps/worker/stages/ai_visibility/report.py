"""Pure builder for the report's AI Visibility section.

Turns normalized :class:`AiVisibilityFacts` into a deterministic presentation payload: a headline
visibility score + metric tiles, the per-LLM distribution, top topics, competitors, and by-country
rows. It is *presentation only* — it never changes a score (scoring invariant) — and returns
``None`` when there is nothing to show, so the report renders byte-identically when AI-visibility
enrichment did not run.
"""

from __future__ import annotations

import math
from typing import Any

from apps.worker.stages.ai_visibility.schema import AiVisibilityFacts

JsonDict = dict[str, Any]

_RENDERABLE_STATUSES = frozenset({"complete", "partial"})
# Statuses that render a visible "could not retrieve" note rather than nothing at all. "empty" is
# included because a silent no-section is indistinguishable from a broken feature — the Multilogin
# interstitial presented exactly that way in production. Config skips stay invisible.
_UNAVAILABLE_STATUSES = frozenset({"failed", "empty"})


def _unavailable_message(reason: str | None) -> str:
    """Client-facing note for a run that couldn't return AI-visibility data."""
    if reason == "no_usable_data":
        # CLIENT-FACING copy (ships in the customer's PDF): outcome only, no operator instructions.
        return (
            "No AI Visibility data is available for this domain yet, so this section could not be "
            "populated for this report."
        )
    if reason == "no_session":
        return (
            "Semrush is not connected (or the saved sign-in has expired), so AI Visibility data is "
            "unavailable. Reconnect Semrush once to enable this section on future reports."
        )
    if reason == "captcha":
        return (
            "A CAPTCHA / human-verification step appeared during the automated Semrush sign-in, "
            "so AI Visibility data could not be retrieved for this report."
        )
    if reason == "login_blocked":
        return (
            "The automated Semrush sign-in was blocked, so AI Visibility data could not be "
            "retrieved for this report."
        )
    return "AI Visibility data could not be retrieved for this report at this time."


def _unavailable_section(facts: AiVisibilityFacts) -> JsonDict:
    """A visible 'could not retrieve' section (empty of data) so a blocked run isn't invisible."""
    return {
        "status": "failed",
        "unavailable": True,
        "message": _unavailable_message(facts.reason),
        "reason": facts.reason,
        "provider": facts.provider,
        "domain": facts.domain,
        "retrieved_at": facts.retrieved_at,
        "visibility_score": None,
        "visibility_band": None,
        "metrics": [],
        "per_platform": [],
        "topics": [],
        "competitors": [],
        "by_country": [],
    }


def _coerce_facts(facts: JsonDict | AiVisibilityFacts | None) -> AiVisibilityFacts | None:
    if isinstance(facts, AiVisibilityFacts):
        return facts
    if not isinstance(facts, dict):
        return None
    try:
        return AiVisibilityFacts.model_validate(facts)
    except Exception:
        return None


def _pct_display(value: float | None) -> str | None:
    # Reject None AND non-finite (NaN/±inf) — pydantic float fields accept inf/nan by default, and
    # int(inf)/int(nan) would raise inside this pure builder and sink the whole render.
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    # Half-up to one decimal (repo convention: int(x + 0.5), never banker's round()). Shares are
    # >= 0, so the simple form is correct. Whole numbers render without a trailing ".0".
    rounded = int(float(value) * 10 + 0.5) / 10
    return f"{int(rounded)}%" if rounded == int(rounded) else f"{rounded}%"


def build_ai_visibility_report_data(
    ai_visibility_facts: JsonDict | AiVisibilityFacts | None,
) -> JsonDict | None:
    """Compose the AI Visibility section, or ``None`` if there is nothing to render."""
    facts = _coerce_facts(ai_visibility_facts)
    if facts is None:
        return None
    # A run that reached Semrush but was blocked (CAPTCHA / login wall / error) renders a visible
    # note instead of vanishing — the operator asked for the block to be shown, not hidden.
    if facts.status in _UNAVAILABLE_STATUSES:
        return _unavailable_section(facts)
    if facts.status not in _RENDERABLE_STATUSES:
        return None

    # Metric tiles — only the ones actually present (a missing panel is dropped, never shown as 0).
    metrics: list[JsonDict] = []
    for key, label, value in (
        ("mentions", "Mentions", facts.mentions),
        ("citations", "Citations", facts.citations),
        ("cited_pages", "Cited Pages", facts.cited_pages),
    ):
        if value is not None:
            metrics.append({"key": key, "label": label, "value": value})
    if facts.share_of_voice_pct is not None:
        metrics.append(
            {
                "key": "share_of_voice",
                "label": "Share of Voice",
                "value": _pct_display(facts.share_of_voice_pct),
            }
        )

    per_platform = [
        {
            "platform": p.platform,
            "mentions": p.mentions,
            "share_pct": p.share_pct,
            "share_display": _pct_display(p.share_pct),
        }
        for p in facts.per_platform
        if p.platform
    ]
    topics = [
        {
            "topic": t.topic,
            "visibility": t.visibility,
            "your_mentions": t.your_mentions,
            "ai_volume": t.ai_volume,
        }
        for t in facts.topics
        if t.topic
    ]
    competitors = [
        {
            "label": c.label,
            "visibility_score": c.visibility_score,
            "mentions": c.mentions,
        }
        for c in facts.competitors
        if c.label
    ]
    by_country = [
        {
            "country": c.country,
            "mentions": c.mentions,
            "share_pct": c.share_pct,
            "share_display": _pct_display(c.share_pct),
        }
        for c in facts.by_country
        if c.country
    ]

    # If literally nothing renderable survived (e.g. status was forced to partial with no data),
    # return None so no empty section appears.
    if (
        facts.visibility_score is None
        and not metrics
        and not per_platform
        and not topics
        and not competitors
        and not by_country
    ):
        return None

    return {
        "status": facts.status,
        "provider": facts.provider,
        "domain": facts.domain,
        "retrieved_at": facts.retrieved_at,
        "visibility_score": facts.visibility_score,
        "visibility_band": facts.visibility_band,
        "metrics": metrics,
        "per_platform": per_platform,
        "topics": topics,
        "competitors": competitors,
        "by_country": by_country,
    }
