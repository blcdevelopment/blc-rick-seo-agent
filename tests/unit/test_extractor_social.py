import json
from datetime import UTC, datetime
from pathlib import Path

from apps.worker.stages.social.extractor import _handle_key, extract_social_facts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 6, 23, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _entry(name: str, handle: str) -> dict:
    return {"platform": "instagram", "handle": handle, "raw": _load(name)}


def test_strong_profile_summary() -> None:
    facts = extract_social_facts([_entry("social_instagram_strong.json", "acmestudio")], now=NOW)
    assert facts["status"] == "complete"
    s = facts["summary"]
    assert s["platforms_audited"] == 1
    assert s["total_followers"] == 5000
    assert s["profiles_complete_pct"] == 100
    assert s["avg_posts_per_month"] == 10.1  # 6 posts over an 18-day span (30.44-day month)
    assert s["days_since_last_post"] == 2
    assert s["avg_engagement_rate_pct"] == 5.33
    assert s["profiles_with_link_in_bio"] == 1
    assert s["profiles_with_cta"] == 1
    assert s["has_video_content"] is True
    assert s["profiles_with_logo_avatar"] == 1


def test_weak_profile_summary() -> None:
    facts = extract_social_facts([_entry("social_instagram_weak.json", "weakco")], now=NOW)
    assert facts["status"] == "complete"
    s = facts["summary"]
    assert s["profiles_complete_pct"] == 0
    assert s["avg_posts_per_month"] == 5.1
    assert s["days_since_last_post"] == 83
    assert s["avg_engagement_rate_pct"] == 3.75
    assert s["profiles_with_link_in_bio"] == 0
    assert s["profiles_with_cta"] == 0
    assert s["has_video_content"] is False


def test_combined_aggregation() -> None:
    facts = extract_social_facts(
        [
            _entry("social_instagram_strong.json", "acmestudio"),
            _entry("social_instagram_weak.json", "weakco"),
        ],
        now=NOW,
    )
    assert facts["status"] == "complete"
    s = facts["summary"]
    assert s["platforms_audited"] == 2
    assert s["profiles_complete_pct"] == 50
    assert s["avg_posts_per_month"] == 7.6
    assert s["days_since_last_post"] == 2
    assert s["avg_engagement_rate_pct"] == 4.54
    assert s["profiles_with_link_in_bio"] == 1
    assert s["has_video_content"] is True


def test_facebook_profile_normalizes() -> None:
    facts = extract_social_facts(
        [
            {
                "platform": "facebook",
                "handle": "acmestudio",
                "raw": _load("social_facebook_strong.json"),
            }
        ],
        now=NOW,
    )
    assert facts["status"] == "complete"
    profile = facts["platforms"][0]
    assert profile["platform"] == "facebook"
    assert profile["followers"] == 4200
    assert profile["profile_complete"] is True
    assert profile["link_in_bio"] == "https://acmestudio.example"
    assert profile["has_cta"] is True
    # SAE-6: the FB Pages actor's public phone/address/about are now extracted (were discarded).
    assert profile["phone"] == "+1 (555) 100-2000"
    assert profile["address"] == "123 Main St, Austin, TX 78701"
    assert profile["bio_text"] == "Independent design studio. Message us to book a consult."
    # No posts from the FB pages actor -> cadence/engagement unknown (skip in scoring).
    assert profile["posts_per_month"] is None
    assert profile["avg_engagement_rate_pct"] is None


def test_handle_consistency_across_platforms() -> None:
    # SAE-7: same brand key on both profiles (case/format-insensitive) -> consistent.
    facts = extract_social_facts(
        [
            _entry("social_instagram_strong.json", "acmestudio"),
            {
                "platform": "facebook",
                "handle": "AcmeStudio",
                "raw": _load("social_facebook_strong.json"),
            },
        ],
        now=NOW,
    )
    assert facts["summary"]["handles_consistent"] is True


def test_handle_inconsistency_is_flagged() -> None:
    facts = extract_social_facts(
        [
            _entry("social_instagram_strong.json", "acmestudio"),
            {
                "platform": "facebook",
                "handle": "acme-builders-official",
                "raw": _load("social_facebook_strong.json"),
            },
        ],
        now=NOW,
    )
    assert facts["summary"]["handles_consistent"] is False


def test_single_profile_handle_consistency_is_none() -> None:
    # Fewer than two comparable handles -> nothing to compare -> None (the rule skip-rescales).
    facts = extract_social_facts([_entry("social_instagram_strong.json", "acme")], now=NOW)
    assert facts["summary"]["handles_consistent"] is None


def test_substantive_bio_and_category_coverage() -> None:
    # SAE-8/9: strong profile has a real bio + a declared category; weak has neither.
    strong = extract_social_facts([_entry("social_instagram_strong.json", "acme")], now=NOW)
    assert strong["summary"]["profiles_with_substantive_bio"] == 1
    assert strong["summary"]["category_coverage_pct"] == 100.0
    weak = extract_social_facts([_entry("social_instagram_weak.json", "weak")], now=NOW)
    assert weak["summary"]["profiles_with_substantive_bio"] == 0
    assert weak["summary"]["category_coverage_pct"] == 0.0


def test_youtube_only_category_coverage_is_none() -> None:
    # YouTube has no business-category concept; a YouTube-only audit yields None (rule skips),
    # never a vacuous 0% that would false-fail the category rule.
    facts = extract_social_facts(
        [{"platform": "youtube", "handle": "acme", "raw": _load("social_youtube_strong.json")}],
        now=NOW,
    )
    assert facts["summary"]["category_coverage_pct"] is None


def test_facebook_with_posts_computes_cadence_and_engagement() -> None:
    # When the Facebook Posts actor data is merged in (collector adds raw["posts"]), FB gets
    # the same cadence/recency/engagement treatment as Instagram (was None with page-only data).
    raw = {
        "pageName": "Acme Builders",
        "intro": "Custom homes",
        "website": "https://acmebuilders.example",
        "followers": 4000,
        "messenger": "m.me/acme",
        "profilePhoto": "https://x/y.jpg",
        "posts": [
            {"time": "2026-06-03T00:00:00Z", "likes": 100, "comments": 20},
            {"time": "2026-06-09T00:00:00Z", "likes": 100, "comments": 20},
            {"time": "2026-06-15T00:00:00Z", "likes": 100, "comments": 20},
            {"time": "2026-06-21T00:00:00Z", "likes": 100, "comments": 20},
        ],
    }
    facts = extract_social_facts([{"platform": "facebook", "handle": "acme", "raw": raw}], now=NOW)
    p = facts["platforms"][0]
    assert p["platform"] == "facebook"
    assert p["followers"] == 4000
    assert p["posts_sampled"] == 4
    assert p["posts_per_month"] is not None  # 4 posts over an 18-day span
    assert p["days_since_last_post"] == 2  # newest post 2026-06-21 -> now 2026-06-23
    assert p["avg_engagement_rate_pct"] == 3.0  # (100 + 20) / 4000 followers


def test_instagram_plus_facebook_aggregates_both() -> None:
    facts = extract_social_facts(
        [
            {
                "platform": "instagram",
                "handle": "acmestudio",
                "raw": _load("social_instagram_strong.json"),
            },
            {
                "platform": "facebook",
                "handle": "acmestudio",
                "raw": _load("social_facebook_strong.json"),
            },
        ],
        now=NOW,
    )
    assert facts["status"] == "complete"
    summary = facts["summary"]
    assert summary["platforms_audited"] == 2
    assert summary["profiles_with_link_in_bio"] == 2
    # Cadence/engagement come from the IG profile only (FB has no posts).
    assert summary["avg_posts_per_month"] == 10.1
    assert summary["days_since_last_post"] == 2


def test_youtube_channel_normalizes() -> None:
    facts = extract_social_facts(
        [
            {
                "platform": "youtube",
                "handle": "acmebuilders",
                "raw": _load("social_youtube_strong.json"),
            }
        ],
        now=NOW,
    )
    assert facts["status"] == "complete"
    p = facts["platforms"][0]
    assert p["platform"] == "youtube"
    assert p["followers"] == 5000  # subscriberCount
    assert p["posts_count"] == 120  # videoCount
    assert p["profile_complete"] is True
    assert p["link_in_bio"] == "https://acmebuilders.example"  # parsed from description
    assert p["has_cta"] is True
    assert p["has_video"] is True
    assert p["posts_per_month"] == 10.1  # 6 uploads over an 18-day span (30.44-day month)
    assert p["days_since_last_post"] == 2
    assert p["avg_engagement_rate_pct"] == 5.0  # (220 likes + 30 comments) / 5000 followers


def test_instagram_plus_youtube_aggregates_two_platforms() -> None:
    facts = extract_social_facts(
        [
            {
                "platform": "instagram",
                "handle": "acmestudio",
                "raw": _load("social_instagram_strong.json"),
            },
            {
                "platform": "youtube",
                "handle": "acmebuilders",
                "raw": _load("social_youtube_strong.json"),
            },
        ],
        now=NOW,
    )
    assert facts["status"] == "complete"
    summary = facts["summary"]
    assert summary["platforms_audited"] == 2  # boosts the social.coverage.platforms rule
    assert summary["has_video_content"] is True


def test_youtube_hidden_engagement_skips_instead_of_zero() -> None:
    # The live API omits likeCount/commentCount when a channel hides them. They must be
    # treated as UNKNOWN (rate None -> rule skip_if_missing-rescales), NOT scored as zero
    # engagement (which would falsely fail the engagement rule and dock ~14 points).
    facts = extract_social_facts(
        [
            {
                "platform": "youtube",
                "handle": "hiddenco",
                "raw": _load("social_youtube_hidden.json"),
            }
        ],
        now=NOW,
    )
    assert facts["status"] == "complete"
    p = facts["platforms"][0]
    assert p["followers"] == 2000
    assert p["avg_engagement_rate_pct"] is None  # hidden -> unknown, not 0
    assert p["posts_per_month"] is not None  # cadence still derived from publishedAt
    assert facts["summary"]["avg_engagement_rate_pct"] is None


def test_instagram_content_and_performance_detail() -> None:
    facts = extract_social_facts([_entry("social_instagram_strong.json", "acme")], now=NOW)
    p = facts["platforms"][0]
    # 1 of 6 sampled posts is a Video -> content mix.
    assert p["video_share_pct"] == 16.7
    assert p["image_share_pct"] == 83.3
    assert p["carousel_share_pct"] == 0.0
    assert p["follows_count"] == 310
    assert p["follower_following_ratio"] == 16.1  # 5000 / 310
    assert p["avg_hashtags_per_post"] == 5.0
    assert p["posts_with_cta_caption_pct"] == 100.0  # every caption has a "book a consult" CTA
    assert p["max_posting_gap_days"] is not None
    assert len(p["top_posts"]) == 3
    assert p["top_posts"][0]["views"] == 4000  # the video has the most views
    s = facts["summary"]
    assert s["profiles_business_account"] == 1
    assert s["video_share_pct"] == 16.7
    assert s["avg_hashtags_per_post"] == 5.0


def test_weak_instagram_has_no_hashtags_or_cta() -> None:
    facts = extract_social_facts([_entry("social_instagram_weak.json", "weak")], now=NOW)
    s = facts["summary"]
    assert (
        s["avg_hashtags_per_post"] == 0.0
    )  # present-but-zero -> the hashtag rule fails (not skip)
    assert s["posts_with_cta_caption_pct"] == 0.0
    assert s["profiles_business_account"] == 0


def test_youtube_surfaces_lifetime_views_and_titles() -> None:
    facts = extract_social_facts(
        [
            {
                "platform": "youtube",
                "handle": "acmebuilders",
                "raw": _load("social_youtube_strong.json"),
            }
        ],
        now=NOW,
    )
    p = facts["platforms"][0]
    assert p["total_views"] is not None and p["total_views"] > 0  # channel lifetime views
    assert p["avg_views_per_post"] is not None
    assert p["video_share_pct"] == 100.0
    assert p["top_posts"] and p["top_posts"][0]["title"]  # YouTube videos carry titles
    assert facts["summary"]["total_views"] == p["total_views"]


def test_youtube_business_account_is_unknown_not_zero() -> None:
    # YouTube has no Business/Creator-account concept, so is_business must be None (unknown), and
    # a YouTube-only audit's profiles_business_account must be None so the scored rule
    # skip_if_missing-rescales instead of vacuously docking the channel for a setting it can't have.
    facts = extract_social_facts(
        [
            {
                "platform": "youtube",
                "handle": "acmebuilders",
                "raw": _load("social_youtube_strong.json"),
            }
        ],
        now=NOW,
    )
    assert facts["platforms"][0]["is_business"] is None
    assert facts["summary"]["profiles_business_account"] is None


def test_dormant_account_max_gap_counts_trailing_silence() -> None:
    # The weak account posted twice 12 days apart, then went silent for 83 days. The longest gap
    # must reflect the trailing silence (>= days_since_last_post), so the posting-consistency rule
    # fails rather than praising a clearly-dormant feed as "posting regularly".
    facts = extract_social_facts([_entry("social_instagram_weak.json", "weak")], now=NOW)
    p = facts["platforms"][0]
    assert p["max_posting_gap_days"] >= p["days_since_last_post"] == 83
    assert facts["summary"]["max_posting_gap_days"] >= 83


def test_no_handles_is_skipped() -> None:
    facts = extract_social_facts([], now=NOW)
    assert facts["status"] == "skipped"
    assert facts["platforms"] == []


def test_failed_fetch_is_failed() -> None:
    facts = extract_social_facts([{"platform": "instagram", "handle": "x", "raw": None}], now=NOW)
    assert facts["status"] == "failed"


def test_partial_when_one_profile_fails() -> None:
    facts = extract_social_facts(
        [
            _entry("social_instagram_strong.json", "acmestudio"),
            {"platform": "instagram", "handle": "x", "raw": None},
        ],
        now=NOW,
    )
    assert facts["status"] == "partial"
    assert facts["summary"]["platforms_audited"] == 1


def _fb_entry(posts: list[dict]) -> dict:
    raw = {"pageName": "Acme Builders", "followers": 4000, "posts": posts}
    return {"platform": "facebook", "handle": "acme", "raw": raw}


def test_facebook_text_posts_do_not_claim_image_or_video_mix() -> None:
    # The Posts actor's generic viewsCount is reach, not video views: a text/link post must not
    # be classified as video (a scored fact), and its kind is unknowable, so the image/carousel
    # shares must be None rather than claiming "Image 100%" for a page that posted no images.
    facts = extract_social_facts(
        [
            _fb_entry(
                [
                    {
                        "time": "2026-06-15T00:00:00Z",
                        "likes": 10,
                        "comments": 2,
                        "viewsCount": 500,
                        "text": "We build custom homes",
                    },
                    {
                        "time": "2026-06-21T00:00:00Z",
                        "likes": 8,
                        "comments": 1,
                        "text": "Now booking spring projects",
                    },
                ]
            )
        ],
        now=NOW,
    )
    p = facts["platforms"][0]
    assert p["video_share_pct"] == 0.0
    assert p["image_share_pct"] is None and p["carousel_share_pct"] is None
    assert p["avg_views_per_post"] is None  # reach on a non-video post is not video views
    assert p["has_video"] is False


def test_facebook_flagged_video_still_counts() -> None:
    facts = extract_social_facts(
        [
            _fb_entry(
                [
                    {
                        "time": "2026-06-21T00:00:00Z",
                        "likes": 8,
                        "comments": 1,
                        "isVideo": True,
                        "viewsCount": 900,
                        "text": "Job site tour",
                    }
                ]
            )
        ],
        now=NOW,
    )
    p = facts["platforms"][0]
    assert p["video_share_pct"] == 100.0
    assert p["avg_views_per_post"] == 900.0
    assert p["has_video"] is True


def test_hashtag_counting_requires_a_letter() -> None:
    # "#1" is a ranking claim, not a hashtag — an account using no real hashtags must not earn
    # partial credit on the hashtag rule from it.
    facts = extract_social_facts(
        [
            _fb_entry(
                [
                    {
                        "time": "2026-06-21T00:00:00Z",
                        "likes": 8,
                        "comments": 1,
                        "text": "Voted #1 builder in town",
                    },
                    {
                        "time": "2026-06-15T00:00:00Z",
                        "likes": 6,
                        "comments": 1,
                        "text": "Proudly serving #Austin since 2010",
                    },
                ]
            )
        ],
        now=NOW,
    )
    assert facts["platforms"][0]["avg_hashtags_per_post"] == 0.5  # only #Austin counts


def test_missing_instagram_business_flag_is_unknown_not_personal() -> None:
    # A payload that omits isBusinessAccount means the scraper didn't report the setting — the
    # profile must read as unknown (None), not as a personal account that fails the scored rule.
    raw = _load("social_instagram_strong.json")
    raw.pop("isBusinessAccount", None)
    facts = extract_social_facts([{"platform": "instagram", "handle": "a", "raw": raw}], now=NOW)
    assert facts["platforms"][0]["is_business"] is None
    assert facts["summary"]["profiles_business_account"] is None


def test_zero_view_video_is_reported_not_hidden() -> None:
    # A brand-new video's real 0 views is data: it counts in the average and renders as 0.
    facts = extract_social_facts(
        [
            _fb_entry(
                [
                    {
                        "time": "2026-06-21T00:00:00Z",
                        "likes": 5,
                        "comments": 0,
                        "isVideo": True,
                        "viewsCount": 0,
                        "text": "Fresh upload",
                    },
                    {
                        "time": "2026-06-15T00:00:00Z",
                        "likes": 3,
                        "comments": 0,
                        "isVideo": True,
                        "viewsCount": 300,
                        "text": "Older upload",
                    },
                ]
            )
        ],
        now=NOW,
    )
    p = facts["platforms"][0]
    assert p["avg_views_per_post"] == 150.0
    assert any(tp["views"] == 0 for tp in p["top_posts"])


def test_future_dated_post_clamps_to_zero_days() -> None:
    # Scheduled posts / provider clock skew can put the newest timestamp after "now"; recency and
    # the trailing posting gap must clamp at 0, never render "-1 days".
    facts = extract_social_facts(
        [
            _fb_entry(
                [
                    {
                        "time": "2026-06-23T12:00:00Z",
                        "likes": 5,
                        "comments": 1,
                        "text": "Scheduled",
                    }
                ]
            )
        ],
        now=NOW,
    )
    p = facts["platforms"][0]
    assert p["days_since_last_post"] == 0
    assert p["max_posting_gap_days"] == 0


def test_top_posts_rank_by_combined_attention() -> None:
    # A barely-watched video must not outrank a heavily-engaged post just because it has views.
    facts = extract_social_facts(
        [
            _fb_entry(
                [
                    {
                        "time": "2026-06-15T00:00:00Z",
                        "likes": 10000,
                        "comments": 50,
                        "text": "Finished project reveal",
                    },
                    {
                        "time": "2026-06-21T00:00:00Z",
                        "likes": 4,
                        "comments": 2,
                        "isVideo": True,
                        "viewsCount": 5,
                        "text": "Quick clip",
                    },
                ]
            )
        ],
        now=NOW,
    )
    top = facts["platforms"][0]["top_posts"]
    assert top[0]["engagement"] == 10050  # the engaged post wins the attention proxy


def test_video_share_aggregation_excludes_youtube() -> None:
    # YouTube uploads are definitionally 100% video; the channel must not drag the scored
    # video-share fact up for the feed platforms (or make the rule vacuously pass).
    facts = extract_social_facts(
        [
            _entry("social_instagram_weak.json", "weak"),
            {
                "platform": "youtube",
                "handle": "acme",
                "raw": _load("social_youtube_strong.json"),
            },
        ],
        now=NOW,
    )
    ig = next(p for p in facts["platforms"] if p["platform"] == "instagram")
    yt = next(p for p in facts["platforms"] if p["platform"] == "youtube")
    assert yt["video_share_pct"] == 100.0
    assert facts["summary"]["video_share_pct"] == ig["video_share_pct"]


def test_youtube_only_video_share_is_unscored() -> None:
    facts = extract_social_facts(
        [{"platform": "youtube", "handle": "acme", "raw": _load("social_youtube_strong.json")}],
        now=NOW,
    )
    assert facts["summary"]["video_share_pct"] is None


def test_handle_key_url_forms_collapse_to_the_vanity_handle() -> None:
    # Auto-discovery (and the operator form) store full canonical profile URLs — every vanity
    # form of one brand must produce the same key, or handles_consistent false-fails on the
    # pipeline's most common input shape.
    urls = [
        "AcmeStudio",
        "@acmestudio",
        "https://www.instagram.com/acme.studio/",
        "https://www.facebook.com/AcmeStudio/",
        "https://www.youtube.com/@AcmeStudio",
        "https://www.youtube.com/c/AcmeStudio",
        "https://www.facebook.com/pages/Acme-Studio/123456",
        # Modern FB business-page forms: /people/<Name>/<id>, legacy /pg/<name>, and the
        # directory form /pages/category/<Category>/<Name-ID>/ (long numeric id stripped).
        "https://www.facebook.com/people/Acme-Studio/61550001112223/",
        "https://www.facebook.com/pg/AcmeStudio",
        "https://www.facebook.com/pages/category/General-Contractor/Acme-Studio-104502341234567/",
    ]
    assert {_handle_key(url) for url in urls} == {"acmestudio"}


def test_handle_key_opaque_ids_drop_out_of_the_comparison() -> None:
    # profile.php ids and raw channel ids aren't brand-chosen handles; they must return ""
    # (excluded) rather than a junk key that fails — or accidentally passes — consistency.
    assert _handle_key("https://www.facebook.com/profile.php?id=1234567") == ""
    assert _handle_key("https://www.youtube.com/channel/UCabc123") == ""
    assert _handle_key("") == ""
    assert _handle_key(None) == ""


def test_url_handle_fallback_passes_full_url_through() -> None:
    # Auto-discovery (and a pasted link) supplies the handle AS a full profile URL. When the
    # provider payload carries no url of its own, the fallback must keep that URL verbatim —
    # not nest it under the platform host into a doubled-domain URL.
    from apps.worker.stages.social.extractor import (
        normalize_facebook_profile,
        normalize_instagram_profile,
        normalize_youtube_channel,
    )

    ig = normalize_instagram_profile({}, "https://www.instagram.com/acmestudio/", now=NOW)
    assert ig["url"] == "https://www.instagram.com/acmestudio/"

    fb = normalize_facebook_profile({}, "https://www.facebook.com/acmestudio/", now=NOW)
    assert fb["url"] == "https://www.facebook.com/acmestudio/"

    yt_url = "https://www.youtube.com/channel/UC" + "x" * 22
    yt = normalize_youtube_channel({}, yt_url, now=NOW)
    assert yt["url"] == yt_url

    # Bare handles keep the canonical platform-host form they always had.
    assert (
        normalize_instagram_profile({}, "@acmestudio", now=NOW)["url"]
        == "https://www.instagram.com/acmestudio/"
    )
    assert (
        normalize_youtube_channel({}, "@acmestudio", now=NOW)["url"]
        == "https://www.youtube.com/@acmestudio"
    )


def test_handle_key_normalizes_modern_facebook_p_form() -> None:
    # facebook.com/p/<Name>-<id> is the URL Facebook serves for pages without a vanity
    # username; it must key to the brand, not the junk marker "p".
    assert _handle_key("https://www.facebook.com/p/Acme-Studio-61550001112223/") == "acmestudio"


def test_profile_link_from_handle_detects_scheme_less_links() -> None:
    # THE one URL-shaped-handle detector: scheme'd, protocol-relative, and scheme-less dotted
    # hosts are links; a dotted bare HANDLE (dots are legal in IG usernames) is not.
    from apps.worker.stages.social.extractor import profile_link_from_handle

    assert profile_link_from_handle("www.instagram.com/acme") == "https://www.instagram.com/acme"
    assert profile_link_from_handle("//instagram.com/acme") == "https://instagram.com/acme"
    assert profile_link_from_handle("https://instagram.com/acme") == "https://instagram.com/acme"
    assert profile_link_from_handle("acme.studio") is None
    assert profile_link_from_handle("@acme") is None


def test_youtube_channel_url_keeps_scheme_less_link_handles() -> None:
    from apps.worker.stages.social.extractor import normalize_youtube_channel

    yt = normalize_youtube_channel({}, "www.youtube.com/c/AcmeStudio", now=NOW)
    assert yt["url"] == "https://www.youtube.com/c/AcmeStudio"


def test_handle_key_scheme_less_links_and_post_permalinks() -> None:
    # Scheme-less links are still links (the shared detector) — keyed by the vanity segment,
    # not as raw text; an Instagram post permalink names a post, not the account, so it drops
    # out of the comparison instead of keying on its shortcode ('p' is a marker only on
    # Facebook hosts).
    assert _handle_key("www.instagram.com/acmestudio") == "acmestudio"
    assert _handle_key("//www.facebook.com/AcmeStudio/") == "acmestudio"
    assert _handle_key("https://www.instagram.com/p/Cxyz12345/") == ""
    assert _handle_key("https://www.instagram.com/reel/Cxyz12345/") == ""
    # A genuine short digit-suffixed vanity handle is NOT an FB page id (those are 9+ digits)
    # and must key identically to its bare form, never be corrupted by the id strip.
    assert _handle_key("https://www.facebook.com/pg/Acme-12345") == "acme12345"
    assert _handle_key("@Acme-12345") == "acme12345"


def test_connected_channel_matches_gates_identity() -> None:
    # The Analytics API only reports on the CONNECTED account's channel (ids=channel==MINE):
    # its private metrics may attach only when the audited handle IS that channel — matched by
    # channel id or vanity @handle; anything unresolvable is a mismatch (conservative, like
    # the Places website_mismatch gate).
    from apps.worker.stages.social.extractor import connected_channel_matches

    own = {"id": "UCacme0000000000000000000", "custom_url": "@AcmeStudio", "title": "Acme"}
    assert connected_channel_matches("@acmestudio", own)
    assert connected_channel_matches("https://www.youtube.com/@AcmeStudio", own)
    assert connected_channel_matches("https://www.youtube.com/c/AcmeStudio", own)
    assert connected_channel_matches(
        "https://www.youtube.com/channel/UCacme0000000000000000000", own
    )
    assert connected_channel_matches("UCacme0000000000000000000", own)
    assert not connected_channel_matches("@otherbrand", own)
    assert not connected_channel_matches("@acmestudio", {"id": "", "custom_url": "", "title": "X"})
    assert not connected_channel_matches("", own)
    assert not connected_channel_matches("@acmestudio", None)


def test_handle_key_matches_every_facebook_host() -> None:
    # Facebook serves the same profile paths from www./m./web./mbasic./business. and every
    # locale host. A www-only host gate would miss those and the marker walk would fall back
    # to the MARKER WORD ("pages"), false-failing the scored handle-consistency rule for a
    # business whose handles are in fact identical.
    for host in (
        "www.facebook.com",
        "web.facebook.com",
        "m.facebook.com",
        "es-la.facebook.com",
        "business.facebook.com",
    ):
        assert _handle_key(f"https://{host}/pages/Acme-Builders/104502341234567") == "acmebuilders"
    assert _handle_key("https://web.facebook.com/pg/AcmeBuilders") == "acmebuilders"


def test_profile_url_name_is_three_valued() -> None:
    # "" (NO_PROFILE_NAME) means "this URL names no handle at all" — distinct from None ("no
    # marker; use your own vanity fallback"). Falling back on the "" case would key and render
    # the marker word itself ("@p") for an IG post permalink.
    from apps.worker.stages.social.extractor import NO_PROFILE_NAME, profile_url_name

    assert profile_url_name("www.instagram.com", ["p", "Cxyz12345"]) == NO_PROFILE_NAME
    assert profile_url_name("www.instagram.com", ["acmestudio"]) is None
    assert profile_url_name("www.facebook.com", ["pg", "AcmeStudio"]) == "AcmeStudio"
    assert profile_url_name("www.facebook.com", ["acmestudio"]) is None
    assert profile_url_name("example.com", ["p", "thing"]) is None
