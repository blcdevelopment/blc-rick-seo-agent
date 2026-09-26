"""Pure builder for the report's Competitor Benchmarking section (P2-26 / SMWA-79).

Turns normalized :class:`BenchmarkFacts` + the audit's own headline scores into a deterministic
presentation payload: for each competitor / industry baseline, the audited score vs the baseline
and the delta per metric. It is *presentation only* — it never changes a score (scoring invariant)
— and returns ``None`` when there is nothing to show, so the report renders byte-identically when
benchmarking did not run.
"""

from __future__ import annotations

from typing import Any

from apps.worker.stages.benchmarking.schema import BENCHMARK_METRICS, BenchmarkFacts

JsonDict = dict[str, Any]

#: Display labels for the benchmarked metrics (keyed by schema.BENCHMARK_METRICS).
_METRIC_LABELS: dict[str, str] = {
    "seo": "SEO",
    "uxui": "UX/UI",
    "lead_gen": "Lead-Gen Readiness",
    "social": "Social",
    "overall": "Overall Readiness",
}

_RENDERABLE_STATUSES = frozenset({"complete", "partial"})


def _coerce_facts(benchmark_facts: JsonDict | BenchmarkFacts | None) -> BenchmarkFacts | None:
    if isinstance(benchmark_facts, BenchmarkFacts):
        return benchmark_facts
    if not isinstance(benchmark_facts, dict):
        return None
    try:
        return BenchmarkFacts.model_validate(benchmark_facts)
    except Exception:
        return None


#: Human-readable label per verdict key. The single source of truth for verdict wording — both the
#: PDF template and the DOCX renderer consume `verdict_label` from the builder rather than each
#: re-mapping the keys (which would drift).
_VERDICT_LABELS: dict[str, str] = {"ahead": "Ahead", "behind": "Behind", "on_par": "On par"}


def _verdict(delta: int) -> str:
    if delta > 0:
        return "ahead"
    if delta < 0:
        return "behind"
    return "on_par"


def build_benchmark_report_data(
    *,
    scores: dict[str, int | None],
    benchmark_facts: JsonDict | BenchmarkFacts | None,
) -> JsonDict | None:
    """Compose the Competitor Benchmarking section, or ``None`` if there is nothing to render.

    ``scores`` maps a metric key (``seo`` / ``uxui`` / ``lead_gen`` / ``social`` / ``overall``) to
    the audited 0–100 score (``None`` if not applicable). A metric is only compared when BOTH the
    audited score and the baseline are present.
    """
    facts = _coerce_facts(benchmark_facts)
    if facts is None or facts.status not in _RENDERABLE_STATUSES or not facts.competitors:
        return None

    rows: list[JsonDict] = []
    for competitor in facts.competitors:
        metrics: list[JsonDict] = []
        for key in BENCHMARK_METRICS:
            your_score = scores.get(key)
            baseline = getattr(competitor, key, None)
            if your_score is None or baseline is None:
                continue
            delta = int(your_score) - int(baseline)
            verdict = _verdict(delta)
            metrics.append(
                {
                    "metric": key,
                    "label": _METRIC_LABELS.get(key, key),
                    "your_score": int(your_score),
                    "baseline": int(baseline),
                    "delta": delta,
                    "delta_display": f"+{delta}" if delta > 0 else str(delta),
                    "verdict": verdict,
                    "verdict_label": _VERDICT_LABELS[verdict],
                }
            )
        if metrics:
            rows.append(
                {
                    "label": competitor.label,
                    "is_industry": competitor.is_industry,
                    "source": competitor.source,
                    "metrics": metrics,
                }
            )

    if not rows:
        return None

    return {
        "status": facts.status,
        "provider": facts.provider,
        "target_url": facts.target_url,
        "niche": facts.niche,
        "competitors": rows,
    }
