"""Social report builder depth (phase2-social-v2): quantified findings, strengths, content
insights, top posts, and the per-platform scorecard — the data that makes the social audit
substantive rather than a one-line 'engagement is low'."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.config import Settings
from apps.worker.stages.scoring import score_social_audit
from apps.worker.stages.social.extractor import extract_social_facts
from apps.worker.stages.social.report import _display_handle, build_social_report_data

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 6, 23, tzinfo=UTC)


def _build(name: str, handle: str) -> dict:
    raw = json.loads((FIXTURES / name).read_text())
    facts = extract_social_facts([{"platform": "instagram", "handle": handle, "raw": raw}], now=NOW)
    breakdown = score_social_audit(facts, Settings())
    return build_social_report_data(
        social_facts=facts,
        social_breakdown=breakdown,
        social_score=breakdown["score"],
        handles={"instagram": handle},
    )


def test_weak_findings_are_quantified_with_targets() -> None:
    report = _build("social_instagram_weak.json", "weak")
    assert report["findings"], "a weak account should surface findings"
    metrics = [f["metric"] for f in report["findings"] if f.get("metric")]
    assert metrics, "findings carry a measured metric line"
    # e.g. "0% (target ≥ 100%)" — the measured value AND the threshold, not a bare label.
    assert any("target" in m for m in metrics)


def test_strong_surfaces_strengths_insights_topposts_and_scorecard() -> None:
    report = _build("social_instagram_strong.json", "acme")
    # A strong account is no longer an empty "no issues" report — passing checks become strengths.
    assert report["strengths"]
    assert all(s.get("label") for s in report["strengths"])

    ci = report["content_insights"]
    assert ci["content_mix"]["video"] == 16.7
    assert ci["avg_hashtags_per_post"] == 5.0
    assert ci["posts_with_cta_caption_pct"] == 100.0

    assert report["top_posts"], "top performing posts are surfaced"
    assert report["top_posts"][0]["views"] == 4000

    assert report["per_platform"] and report["per_platform"][0]["platform"] == "instagram"
    assert report["per_platform"][0]["video_share_pct"] == 16.7


def test_metric_line_handles_percent_and_count_facts() -> None:
    report = _build("social_instagram_weak.json", "weak")
    by_id = {f["id"]: f for f in report["findings"]}
    # profiles_complete_pct -> percent-unit metric
    if "social.profile.complete" in by_id:
        assert "%" in (by_id["social.profile.complete"]["metric"] or "")
    # a count fact (link in bio) -> no spurious unit, still shows the target
    if "social.link_in_bio" in by_id:
        assert "target" in (by_id["social.link_in_bio"]["metric"] or "")


def test_top_posts_ranked_by_combined_attention_proxy() -> None:
    # Views and engagement are incommensurable across platforms; the cross-platform sort uses
    # views + engagement so a 10k-engagement image outranks a 5-view video.
    facts = {
        "status": "complete",
        "summary": {},
        "platforms": [
            {
                "platform": "youtube",
                "status": "complete",
                "handle": "acme",
                "top_posts": [
                    {"type": "video", "views": 5, "likes": 1, "comments": 0, "engagement": 1}
                ],
            },
            {
                "platform": "instagram",
                "status": "complete",
                "handle": "acme",
                "top_posts": [
                    {
                        "type": "image",
                        "views": None,
                        "likes": 9000,
                        "comments": 1000,
                        "engagement": 10000,
                    }
                ],
            },
        ],
    }
    report = build_social_report_data(
        social_facts=facts, social_breakdown={}, social_score=None, handles={}
    )
    assert [p["type"] for p in report["top_posts"]] == ["image", "video"]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("acme", "acme"),
        ("@acme", "acme"),
        ("https://www.instagram.com/acme/", "acme"),
        ("https://facebook.com/acme.builders", "acme.builders"),
        ("https://www.facebook.com/pages/Acme-Builders/123456", "Acme-Builders"),
        ("https://www.facebook.com/profile.php?id=123456", "id 123456"),
        ("https://www.youtube.com/channel/UCabc123", "UCabc123"),
        ("https://www.youtube.com/c/AcmeBuilds", "AcmeBuilds"),
        ("https://www.youtube.com/user/acmebuilds", "acmebuilds"),
        ("https://www.youtube.com/@acme", "acme"),
        ("", ""),
        (None, ""),
    ],
)
def test_display_handle_forms(stored: str | None, expected: str) -> None:
    assert _display_handle(stored) == expected


def test_handles_are_display_cleaned_without_mutating_facts() -> None:
    stored_url = "https://www.instagram.com/acme/"
    facts = {
        "status": "complete",
        "summary": {},
        "platforms": [
            {"platform": "instagram", "status": "complete", "handle": stored_url, "top_posts": []}
        ],
    }
    report = build_social_report_data(
        social_facts=facts,
        social_breakdown={},
        social_score=None,
        handles={"instagram": stored_url},
    )
    assert report["handles"] == {"instagram": "acme"}
    assert report["per_platform"][0]["handle"] == "acme"
    assert report["platforms"][0]["handle"] == "acme"
    # The stored facts keep the canonical URL (the payload works on copies).
    assert facts["platforms"][0]["handle"] == stored_url


def test_display_handle_renders_modern_facebook_forms() -> None:
    # The display parser must understand the same modern FB page-URL forms the consistency key
    # does — not render "@people" / "@pg" / "@category" on every report surface.
    from apps.worker.stages.social.report import _display_handle

    assert (
        _display_handle("https://www.facebook.com/people/Smith-Builders/61550001112223/")
        == "Smith-Builders"
    )
    assert _display_handle("https://www.facebook.com/pg/AcmeStudio") == "AcmeStudio"
    assert (
        _display_handle("https://www.facebook.com/p/Acme-Studio-61550001112223/") == "Acme-Studio"
    )
    assert (
        _display_handle(
            "https://www.facebook.com/pages/category/"
            "General-Contractor/Acme-Studio-104502341234567/"
        )
        == "Acme-Studio"
    )


def test_display_handle_cleans_scheme_less_links() -> None:
    # Scheme-less profile links are links too (profile_link_from_handle): the report must show
    # '@acmestudio', not '@www.instagram.com/acmestudio/'.
    from apps.worker.stages.social.report import _display_handle

    assert _display_handle("www.instagram.com/acmestudio/") == "acmestudio"
    assert _display_handle("//www.youtube.com/@AcmeStudio") == "AcmeStudio"


def test_display_handle_never_renders_a_marker_word() -> None:
    # An IG post permalink pasted as the handle names no account: the surfaces must render the
    # empty-handle placeholder, never "@p" (the same URL keys to "" for scoring, so all three
    # surfaces agree there is no handle).
    from apps.worker.stages.social.report import _display_handle

    assert _display_handle("https://www.instagram.com/p/Cxyz12345/") == ""
    assert _display_handle("https://www.instagram.com/reel/Cxyz12345/") == ""
    # Facebook's marker forms still resolve to the brand on every FB host.
    assert _display_handle("https://web.facebook.com/pages/Acme-Studio/104502341234567") == (
        "Acme-Studio"
    )
