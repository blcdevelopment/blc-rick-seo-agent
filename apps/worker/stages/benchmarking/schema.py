"""Typed schema for the competitor-benchmarking layer (P2-26 / SMWA-79).

A single validated source of truth for the benchmark facts a provider returns. ``extra="forbid"``
makes a typo or a drifted field a hard error rather than a silently-missing value — the same
contract the social schema uses.

The metrics benchmarked are the audit's own headline scores (``seo`` / ``uxui`` / ``lead_gen`` /
``social`` / ``overall``, all 0–100), so a baseline is just those same scores measured for a
competitor domain or an industry cohort. Nothing here feeds the deterministic scoring engine — a
benchmark is *presentation only* (invariant: scores never change), so this layer can never alter
the website / social / overall numbers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

JsonDict = dict[str, Any]

#: The headline metrics a benchmark can compare (all 0–100 scores; higher is better).
BENCHMARK_METRICS: tuple[str, ...] = ("seo", "uxui", "lead_gen", "social", "overall")


class CompetitorBaseline(BaseModel):
    """One baseline row: a competitor domain or an industry cohort, scored on the same metrics.

    Every metric is optional (``None``) because a provider may only supply some of them; the report
    builder simply skips a metric with no baseline rather than inventing a comparison.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    is_industry: bool = False
    source: str | None = None
    seo: int | None = None
    uxui: int | None = None
    lead_gen: int | None = None
    social: int | None = None
    overall: int | None = None


class BenchmarkFacts(BaseModel):
    """Normalized benchmark facts the report builder consumes.

    ``status`` follows the external-source vocabulary (``complete`` | ``partial`` | ``failed`` |
    ``skipped`` | ``empty``); only ``complete`` / ``partial`` with at least one baseline renders a
    section. ``reason`` records why a non-complete run skipped (disabled / no key / not implemented
    yet), surfaced nowhere user-facing but useful in logs and tests.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = "skipped"
    reason: str | None = None
    provider: str | None = None
    target_url: str | None = None
    niche: str | None = None
    competitors: list[CompetitorBaseline] = Field(default_factory=list)

    def as_facts(self) -> JsonDict:
        return self.model_dump()
