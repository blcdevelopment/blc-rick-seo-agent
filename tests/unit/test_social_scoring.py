import json
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.config import Settings
from apps.worker.stages.scoring import score_social_audit
from apps.worker.stages.social.extractor import extract_social_facts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 6, 23, tzinfo=UTC)


def _facts(name: str, handle: str) -> dict:
    raw = json.loads((FIXTURES / name).read_text())
    return extract_social_facts([{"platform": "instagram", "handle": handle, "raw": raw}], now=NOW)


def test_strong_social_score_is_high() -> None:
    result = score_social_audit(_facts("social_instagram_strong.json", "acme"), Settings())
    assert result["status"] == "complete"
    assert result["score"] >= 85
    assert result["category"]["category"] == "social"


def test_weak_social_score_is_low() -> None:
    result = score_social_audit(_facts("social_instagram_weak.json", "weak"), Settings())
    assert result["status"] == "complete"
    assert result["score"] <= 45


def test_strong_beats_weak() -> None:
    strong = score_social_audit(_facts("social_instagram_strong.json", "a"), Settings())["score"]
    weak = score_social_audit(_facts("social_instagram_weak.json", "b"), Settings())["score"]
    assert strong > weak


def test_skipped_collection_yields_no_score() -> None:
    result = score_social_audit({"status": "skipped"}, Settings())
    assert result["status"] == "skipped"
    assert result["score"] is None


def test_youtube_only_audit_skips_business_account_rule() -> None:
    # A YouTube-only audit must not be penalized by the Business/Creator-account rule (YouTube has
    # no such concept) — the rule should skip_if_missing-rescale, not surface a vacuous failure.
    raw = json.loads((FIXTURES / "social_youtube_strong.json").read_text())
    facts = extract_social_facts([{"platform": "youtube", "handle": "acme", "raw": raw}], now=NOW)
    result = score_social_audit(facts, Settings())
    by_id = {r["rule_id"]: r for r in result["category"]["rules"]}
    assert by_id["social.profile.business_account"]["result"] == "skipped"


def test_social_rubric_does_not_change_website_composite() -> None:
    # Guard: allowing category="social" must not let "social" into the website composite.
    from apps.worker.stages.scoring import load_composite_rubric

    composite = load_composite_rubric(Settings().rubric_composite_path)
    assert set(composite.weights) == {"seo", "uxui"}


def test_youtube_only_audit_skips_video_share_rule() -> None:
    # A channel is definitionally 100% video, so a YouTube-only audit must not vacuously pass the
    # video-share rule — the summary fact is None there and the rule skip_if_missing-rescales.
    raw = json.loads((FIXTURES / "social_youtube_strong.json").read_text())
    facts = extract_social_facts([{"platform": "youtube", "handle": "acme", "raw": raw}], now=NOW)
    result = score_social_audit(facts, Settings())
    by_id = {r["rule_id"]: r for r in result["category"]["rules"]}
    assert by_id["social.content.video_share"]["result"] == "skipped"
