"""Typed common schema for the social-audit fact bundle (P2-22 / SMWA-74).

A single, validated source of truth for the ``social.*`` facts that ``rubrics/social.yaml``
scores. Every provider's normalizer (Instagram / Facebook / YouTube) builds the SAME
``SocialProfileFacts`` shape, and ``summarize_profiles`` builds the SAME ``SocialSummary``;
the rubric ``fact_path`` values map 1:1 onto these field names. ``extra="forbid"`` makes a
typo or a drifted field a hard error instead of a silently-missing fact.

The models are intentionally a *contract*, not behaviour — the normalizers in ``extractor``
compute the values and hand them here; ``.model_dump()`` returns the plain dicts the rest of
the pipeline already expects (scoring navigates them by ``fact_path``), so wrapping them
changes nothing observable while guaranteeing the extractor and the rubric stay in lockstep
(see ``tests/unit/test_social_schema.py``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

JsonDict = dict[str, Any]


class SocialProfileFacts(BaseModel):
    """One audited social profile, normalized into platform-independent facts.

    Field names are the ``social.platforms[*].*`` contract; the rubric reads the aggregates
    in :class:`SocialSummary`, but per-profile facts feed those aggregates and the report.
    ``None`` (not ``0``) is used for cadence/recency/engagement when a provider can't supply
    them, so the corresponding ``skip_if_missing`` rules rescale instead of unfairly failing.
    """

    model_config = ConfigDict(extra="forbid")

    platform: str
    handle: str
    url: str
    status: str = "complete"
    followers: int = 0
    posts_count: int = 0
    verified: bool = False
    private: bool = False
    # None for a platform with no Business/Creator-account concept (YouTube), so the scored
    # business-account aggregate rescales it away instead of counting it a failure.
    is_business: bool | None = False
    category: str | None = None
    bio_present: bool = False
    # Raw bio/about/description text (None when the provider returned none). Powers the bio-quality
    # check (SAE-8) and handle/NAP context; kept short and deterministic.
    bio_text: str | None = None
    # Public business contact fields. Instagram's official scraper returns NEITHER phone nor
    # address; Facebook Pages returns them only when the Page exposes them publicly (SAE-6). None
    # when absent, so the NAP cross-check (SAE-10) skips rather than false-failing.
    phone: str | None = None
    address: str | None = None
    link_in_bio: str | None = None
    has_cta: bool = False
    profile_complete: bool = False
    has_logo_avatar: bool = False
    posts_sampled: int = 0
    posts_per_month: float | None = None
    days_since_last_post: int | None = None
    avg_engagement_rate_pct: float | None = None
    has_video: bool = False

    # Content & performance detail derived from the sampled posts (None when not derivable, e.g.
    # a Facebook page with no posts, or hashtags/captions a provider didn't return). These power
    # the report's content-insights / top-posts / per-platform sections and a few of the rules.
    follows_count: int = 0
    follower_following_ratio: float | None = None
    video_share_pct: float | None = None
    image_share_pct: float | None = None
    carousel_share_pct: float | None = None
    max_posting_gap_days: int | None = None
    like_to_comment_ratio: float | None = None
    avg_views_per_post: float | None = None
    total_views: int | None = None
    best_post_engagement: int | None = None
    avg_hashtags_per_post: float | None = None
    posts_with_cta_caption_pct: float | None = None
    top_posts: list[JsonDict] = Field(default_factory=list)

    def as_facts(self) -> JsonDict:
        """The plain-dict form the pipeline consumes (scoring reads it by ``fact_path``)."""
        return self.model_dump()


class SocialSummary(BaseModel):
    """Cross-profile aggregates the social rubric scores (``social.summary.*``).

    Defaults are the empty-audit values, so ``SocialSummary().model_dump()`` is the canonical
    "nothing collected" summary. Cadence/recency/engagement are ``None`` when no profile has
    post data, so the ``skip_if_missing`` rules rescale rather than scoring a false zero.
    """

    model_config = ConfigDict(extra="forbid")

    platforms_audited: int = 0
    total_followers: int = 0
    profiles_complete_pct: int = 0
    avg_posts_per_month: float | None = 0.0
    days_since_last_post: int | None = None
    avg_engagement_rate_pct: float | None = 0.0
    profiles_with_link_in_bio: int = 0
    profiles_with_cta: int = 0
    has_video_content: bool = False
    profiles_with_logo_avatar: int = 0

    # Extended detail aggregates. Those whose name is a rule ``fact_path`` in social.yaml are
    # SCORED (profiles_business_account, video_share_pct, max_posting_gap_days,
    # avg_hashtags_per_post); the rest are surfaced in the report's content-insights section.
    # None (not 0) when no profile supplied the underlying data, so any scored rule skip_if_missing.
    profiles_verified: int = 0
    profiles_business_account: int | None = 0
    profiles_with_category: int = 0
    # SCORED profile-quality/consistency aggregates (SAE-7/8/9/10). None => the corresponding
    # skip_if_missing rule rescales instead of false-failing:
    #   handles_consistent   None when <2 profiles (nothing to compare).
    #   category_coverage_pct None when no feed (non-YouTube) profile carries a category concept.
    #   nap_phone_consistent  None on a standalone social audit (no website phone to compare) —
    #                         set only in the combined path (tasks._augment_with_social).
    handles_consistent: bool | None = None
    profiles_with_substantive_bio: int = 0
    category_coverage_pct: float | None = None
    # Does the declared category fit the audit's niche (SAE-9-full)? None when the niche can't be
    # classified or no category is set -> the relevance rule skips (never a guessed false finding).
    category_matches_niche: bool | None = None
    nap_phone_consistent: bool | None = None
    # Google Business Profile signals (SAE-13), injected in the combined path from the Places API.
    # None on a standalone audit or when no Google listing is found -> the reviews rule skips.
    google_rating: float | None = None
    google_review_count: int | None = None
    avg_follower_following_ratio: float | None = None
    video_share_pct: float | None = None
    image_share_pct: float | None = None
    carousel_share_pct: float | None = None
    max_posting_gap_days: int | None = None
    avg_views_per_post: float | None = None
    total_views: int | None = None
    avg_like_to_comment_ratio: float | None = None
    avg_hashtags_per_post: float | None = None
    posts_with_cta_caption_pct: float | None = None

    def as_facts(self) -> JsonDict:
        return self.model_dump()
