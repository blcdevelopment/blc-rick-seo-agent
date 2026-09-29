"""Typed common social schema + rubric drift guard (P2-22 / SMWA-74)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from apps.worker.stages.social.extractor import _empty_summary
from apps.worker.stages.social.schema import SocialProfileFacts, SocialSummary

RUBRIC = Path(__file__).resolve().parents[2] / "rubrics" / "social.yaml"


def test_empty_summary_is_schema_defaults() -> None:
    # The schema's defaults ARE the canonical empty-audit summary (single source of truth).
    assert _empty_summary() == SocialSummary().model_dump()
    assert _empty_summary()["avg_posts_per_month"] == 0.0
    assert _empty_summary()["days_since_last_post"] is None


def test_profile_schema_forbids_unknown_field() -> None:
    # extra="forbid" => a typo'd / drifted key is a hard error, not a silent missing fact.
    with pytest.raises(ValidationError):
        SocialProfileFacts(platform="instagram", handle="x", url="https://x", bogus_field=1)


def test_summary_fields_cover_every_rubric_fact_path() -> None:
    # If a social.yaml rule references social.summary.X, the schema MUST define field X — this
    # fails loudly the moment the rubric and the extractor's fact contract drift apart.
    rubric = yaml.safe_load(RUBRIC.read_text())
    summary_fields = set(SocialSummary.model_fields)
    for rule in rubric["rules"]:
        path = rule["fact_path"]
        if path.startswith("social.summary."):
            field = path.split("social.summary.", 1)[1]
            assert field in summary_fields, f"rubric fact_path {path} has no SocialSummary field"
        else:
            # The only non-summary fact path the rubric reads is the collection status.
            assert path == "social.status", f"unexpected social fact_path: {path}"


def test_profile_round_trips_through_facts() -> None:
    profile = SocialProfileFacts(
        platform="youtube", handle="@acme", url="https://youtube.com/@acme"
    )
    facts = profile.as_facts()
    assert facts["platform"] == "youtube"
    assert facts["posts_per_month"] is None  # cadence unknown defaults to None, not 0
    assert set(facts) == set(SocialProfileFacts.model_fields)
