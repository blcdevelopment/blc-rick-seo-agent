"""Competitor-benchmarking collector — orchestrates provider fetch -> normalized facts (P2-26).

Graceful by design (matches the PSI / social / external-SEO pattern). It skips — never penalizes,
never aborts, incurs no cost — in every not-ready state:

- ``benchmark_enabled`` is off (the default)      => ``skipped: benchmarking_disabled``
- no vendor selected / unknown vendor             => ``skipped: no_benchmark_provider_selected``
- the selected vendor's API key is missing        => ``skipped: missing_benchmark_api_key``
- the vendor client is not implemented (all today) => ``skipped: provider_not_implemented``

Only when a provider actually returns baseline data does it normalize into :class:`BenchmarkFacts`.
Dispatch is generic over the :mod:`providers` registry, so the collector never names a vendor.
"""

from __future__ import annotations

import math
from typing import Any

from apps.shared.config import Settings
from apps.worker.stages.benchmarking.providers import get_provider
from apps.worker.stages.benchmarking.schema import BenchmarkFacts, CompetitorBaseline
from apps.worker.stages.scoring import round_score

JsonDict = dict[str, Any]


def _skipped(reason: str, provider: str | None = None) -> JsonDict:
    return BenchmarkFacts(status="skipped", reason=reason, provider=provider).as_facts()


def _coerce_metric(value: Any) -> int | None:
    """Coerce a provider-supplied metric to a 0–100 int, or ``None`` if unusable.

    ``bool`` is rejected explicitly (it is an ``int`` subclass) and non-finite floats (NaN / ±inf)
    are treated as unusable (so a stray provider value can't raise inside ``int()``); the numeric
    case reuses the engine's canonical half-up-then-clamp ``scoring.round_score`` so the rounding
    matches the rest of the codebase.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return round_score(float(value), 100)


def normalize_benchmark_facts(
    raw: JsonDict,
    *,
    provider: str,
    target_url: str | None,
    niche: str | None,
) -> JsonDict:
    """Normalize a provider payload (``{"competitors": [...]}``) into typed benchmark facts.

    Pure and deterministic, and defensive about a malformed payload: a non-dict ``raw`` (or a
    non-dict row) is treated as no data (``status: empty``) rather than raising, so a direct caller
    is protected without relying on an outer wrapper. A row missing a usable ``label`` or any
    metric is dropped; if nothing usable survives the run is ``empty`` (no section, no penalty).
    """
    rows = raw.get("competitors") if isinstance(raw, dict) else None
    # A truthy non-list value (e.g. ``competitors: 1``) must degrade like a missing list.
    rows = rows if isinstance(rows, list) else []
    baselines: list[CompetitorBaseline] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        metrics = {
            m: _coerce_metric(row.get(m)) for m in ("seo", "uxui", "lead_gen", "social", "overall")
        }
        if all(v is None for v in metrics.values()):
            continue
        baselines.append(
            CompetitorBaseline(
                label=label,
                is_industry=bool(row.get("is_industry")),
                source=str(row.get("source") or provider),
                **metrics,
            )
        )

    status = "complete" if baselines else "empty"
    return BenchmarkFacts(
        status=status,
        reason=None if baselines else "no_usable_baselines",
        provider=provider,
        target_url=target_url,
        niche=niche,
        competitors=baselines,
    ).as_facts()


def collect_benchmark_facts(
    settings: Settings,
    *,
    target_url: str,
    niche: str | None = None,
    competitors: list[str] | None = None,
) -> JsonDict:
    """Collect competitor/industry baselines, degrading gracefully at every not-ready state."""
    if not settings.benchmark_enabled:
        return _skipped("benchmarking_disabled")

    # Normalize the operator-supplied vendor key (env values often carry stray case/whitespace or a
    # trailing newline) so a valid vendor isn't misread as unselected.
    selected = (settings.benchmark_provider or "").strip().lower()
    provider = get_provider(selected)
    if provider is None:
        return _skipped("no_benchmark_provider_selected", provider=selected or None)
    if not provider.credential_available(settings):
        return _skipped("missing_benchmark_api_key", provider=provider.name)

    raw = provider.fetch(
        target_url=target_url,
        niche=niche,
        competitors=competitors or [],
        settings=settings,
    )
    if raw is None:
        # No payload at all — today this is always the stub no-op (deferred paid client). A live
        # provider that ran but found nothing returns an (empty) dict, which normalizes to `empty`.
        return _skipped("provider_not_implemented", provider=provider.name)

    return normalize_benchmark_facts(
        raw, provider=provider.name, target_url=target_url, niche=niche
    )
