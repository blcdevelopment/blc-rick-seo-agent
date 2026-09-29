"""Typed schema for the AI Visibility enrichment layer (Semrush AI Visibility Toolkit).

The single validated source of truth for the AI-visibility facts a provider returns.
``extra="forbid"`` makes a typo or a drifted field a hard error rather than a silently-missing
value — the same contract the ``benchmarking`` / ``social`` schemas use.

Nothing here feeds the deterministic scoring engine — AI visibility is *presentation only*
(invariant: scores never change), so this layer can never alter the website / social / overall
numbers. Two models:

- :class:`AiVisibilityExtraction` — exactly the fields the vision model reads off the Semrush
  dashboard screenshot (data only; no status/provenance). Used as the OpenAI structured-output
  ``text_format`` so a drifted extraction is rejected and retried at the SDK layer.
- :class:`AiVisibilityFacts` — the normalized facts the collector stores and the report builder
  consumes: the extraction plus status/provenance (``provider`` / ``domain`` / ``retrieved_at``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

JsonDict = dict[str, Any]


class PlatformMention(BaseModel):
    """One row of the "Distribution by LLM" panel: an AI engine and the brand's share of it."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    mentions: int | None = None
    share_pct: float | None = None


class TopicRow(BaseModel):
    """One row of the "Your Performing Topics" table."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    visibility: int | None = None
    your_mentions: int | None = None
    ai_volume: str | None = None  # kept as text ("2.2K", "116.9K") — a display value, not scored


class CompetitorVisibility(BaseModel):
    """One competitor in the comparison set (from the "compareWith" view)."""

    model_config = ConfigDict(extra="forbid")

    label: str
    visibility_score: int | None = None
    mentions: int | None = None


class CountryMention(BaseModel):
    """One row of the "Mentions by Country" panel."""

    model_config = ConfigDict(extra="forbid")

    country: str
    mentions: int | None = None
    share_pct: float | None = None


class AiVisibilityExtraction(BaseModel):
    """The data the vision model extracts from the Semrush AI Visibility dashboard screenshot.

    Every field is optional: the dashboard may not show a panel, or the model may not read one
    confidently. All fields are data-only — no status or provenance (the collector stamps those).
    """

    model_config = ConfigDict(extra="forbid")

    visibility_score: int | None = None  # the gauge, 0–100 ("19/100 Low")
    visibility_band: str | None = None  # the qualitative label next to it ("Low"/"Medium"/"High")
    mentions: int | None = None
    citations: int | None = None
    cited_pages: int | None = None
    share_of_voice_pct: float | None = None
    per_platform: list[PlatformMention] = Field(default_factory=list)
    topics: list[TopicRow] = Field(default_factory=list)
    competitors: list[CompetitorVisibility] = Field(default_factory=list)
    by_country: list[CountryMention] = Field(default_factory=list)


class AiVisibilityFacts(BaseModel):
    """Normalized AI-visibility facts the report builder consumes.

    ``status`` follows the external-source vocabulary (``complete`` | ``partial`` | ``failed`` |
    ``skipped`` | ``empty``); only ``complete`` / ``partial`` with real data renders a section.
    ``reason`` records why a non-complete run skipped (disabled / no credentials / fetch failed),
    surfaced nowhere user-facing but useful in logs and tests.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = "skipped"
    reason: str | None = None
    provider: str | None = None
    domain: str | None = None
    retrieved_at: str | None = None

    visibility_score: int | None = None
    visibility_band: str | None = None
    mentions: int | None = None
    citations: int | None = None
    cited_pages: int | None = None
    share_of_voice_pct: float | None = None
    per_platform: list[PlatformMention] = Field(default_factory=list)
    topics: list[TopicRow] = Field(default_factory=list)
    competitors: list[CompetitorVisibility] = Field(default_factory=list)
    by_country: list[CountryMention] = Field(default_factory=list)

    def as_facts(self) -> JsonDict:
        return self.model_dump()
