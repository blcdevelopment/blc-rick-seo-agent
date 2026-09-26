"""AI Visibility collector — orchestrates provider fetch -> normalized facts.

Graceful by design (matches the PSI / social / benchmarking pattern). It skips — never penalizes,
never aborts, incurs no cost — in every not-ready state:

- ``ai_visibility_enabled`` is off (the default)   => ``skipped: ai_visibility_disabled``
- no provider selected / unknown provider          => ``skipped: no_ai_visibility_provider``
- the provider's credentials are missing           => ``skipped: missing_credentials``
- the provider ran but returned nothing / errored  => ``skipped: fetch_failed``

Only when a provider actually returns extraction data does it normalize into
:class:`AiVisibilityFacts`. Dispatch is generic over the :mod:`providers` registry, so the
collector never names a provider.

The provider fetch (a Playwright login + screenshot + vision extraction) is **injectable** so tests
never touch the network — pass ``fetcher=`` a fake returning a raw extraction dict, exactly like
``run_collection_audit(social_collector=...)``.
"""

from __future__ import annotations

import math
from typing import Any

from apps.shared.config import Settings
from apps.worker.stages.ai_visibility.providers import get_provider
from apps.worker.stages.ai_visibility.schema import AiVisibilityExtraction, AiVisibilityFacts
from apps.worker.stages.scoring import round_score

JsonDict = dict[str, Any]


def _skipped(reason: str, provider: str | None = None) -> JsonDict:
    return AiVisibilityFacts(status="skipped", reason=reason, provider=provider).as_facts()


def _failed(
    reason: str, *, provider: str | None, domain: str | None, retrieved_at: str | None
) -> JsonDict:
    """The bot ran but couldn't get the data (CAPTCHA / login wall / error). Unlike a ``skipped``
    config state, this RENDERS an honest "could not retrieve" note in the report rather than
    silently omitting the section — so a blocked run is visible, not invisible."""
    return AiVisibilityFacts(
        status="failed", reason=reason, provider=provider, domain=domain, retrieved_at=retrieved_at
    ).as_facts()


def _coerce_score(value: Any) -> int | None:
    """Coerce a 0–100 score-like value to an int, or ``None`` if unusable.

    ``bool`` is rejected (it is an ``int`` subclass) and non-finite floats (NaN / ±inf) are treated
    as unusable; the numeric case reuses the engine's canonical half-up-then-clamp
    ``scoring.round_score`` so rounding matches the rest of the codebase.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return round_score(float(value), 100)


def normalize_ai_visibility_facts(
    raw: JsonDict | AiVisibilityExtraction | None,
    *,
    provider: str,
    domain: str | None,
    retrieved_at: str | None,
) -> JsonDict:
    """Normalize a provider extraction into typed AI-visibility facts.

    Pure and deterministic, and defensive about a malformed payload: a non-dict / invalid ``raw`` is
    treated as no data (``status: empty``) rather than raising, so a direct caller is protected
    without relying on an outer wrapper. ``visibility_score`` is clamped to 0–100 via the shared
    rounder; the lists are validated by the extraction model (a bad row is dropped by rebuilding
    from the validated model). If nothing usable survives the run is ``empty`` (no section, no
    penalty).
    """
    if isinstance(raw, AiVisibilityExtraction):
        extraction: AiVisibilityExtraction | None = raw
    elif isinstance(raw, dict):
        try:
            extraction = AiVisibilityExtraction.model_validate(raw)
        except Exception:
            extraction = None
    else:
        extraction = None

    if extraction is None:
        return AiVisibilityFacts(
            status="empty",
            reason="no_usable_data",
            provider=provider,
            domain=domain,
            retrieved_at=retrieved_at,
        ).as_facts()

    facts = AiVisibilityFacts(
        status="complete",
        provider=provider,
        domain=domain,
        retrieved_at=retrieved_at,
        visibility_score=_coerce_score(extraction.visibility_score),
        visibility_band=extraction.visibility_band,
        mentions=extraction.mentions,
        citations=extraction.citations,
        cited_pages=extraction.cited_pages,
        share_of_voice_pct=extraction.share_of_voice_pct,
        per_platform=extraction.per_platform,
        topics=extraction.topics,
        competitors=extraction.competitors,
        by_country=extraction.by_country,
    )

    # "Any usable signal" = a headline scalar or at least one populated panel. Nothing ⇒ empty
    # (no section rendered), so a screenshot the model couldn't read never fabricates a section.
    has_signal = any(
        v is not None
        for v in (
            facts.visibility_score,
            facts.mentions,
            facts.citations,
            facts.cited_pages,
            facts.share_of_voice_pct,
        )
    ) or any((facts.per_platform, facts.topics, facts.competitors, facts.by_country))
    if not has_signal:
        facts.status = "empty"
        facts.reason = "no_usable_data"

    return facts.as_facts()


def collect_ai_visibility_facts(
    settings: Settings,
    *,
    domain: str,
    retrieved_at: str | None = None,
) -> JsonDict:
    """Collect AI-visibility facts for ``domain``, degrading gracefully at every not-ready state."""
    if not settings.ai_visibility_enabled:
        return _skipped("ai_visibility_disabled")

    selected = (settings.ai_visibility_provider or "semrush").strip().lower()
    provider = get_provider(selected)
    if provider is None:
        return _skipped("no_ai_visibility_provider", provider=selected or None)
    if not provider.credential_available(settings):
        return _skipped("missing_credentials", provider=provider.name)

    try:
        raw = provider.fetch(domain=domain, settings=settings)
    except Exception:
        # Last-resort backstop so a bug in the bot can never sink the calling task. Rendered as an
        # honest "could not retrieve" note, not a silent skip.
        return _failed("error", provider=provider.name, domain=domain, retrieved_at=retrieved_at)

    # A CAPTCHA / login wall stopped the bot before the dashboard — surface it as a visible note.
    if isinstance(raw, dict) and raw.get("__blocked__"):
        return _failed(
            str(raw.get("__blocked__")),
            provider=provider.name,
            domain=domain,
            retrieved_at=retrieved_at,
        )
    if raw is None:
        return _failed(
            "unavailable", provider=provider.name, domain=domain, retrieved_at=retrieved_at
        )

    return normalize_ai_visibility_facts(
        raw, provider=provider.name, domain=domain, retrieved_at=retrieved_at
    )
