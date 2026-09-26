"""Deterministic normalization of raw social provider payloads into ``social.*`` facts.

Pure functions only (no network) so the social score is reproducible and unit-testable
from fixtures — mirroring extractor_seo / extractor_uxui. Output keys match the
``fact_path`` values in ``rubrics/social.yaml``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from apps.worker.stages.social.categories import category_relevance
from apps.worker.stages.social.schema import SocialProfileFacts, SocialSummary

JsonDict = dict[str, Any]


def _profile_facts(values: JsonDict) -> JsonDict:
    """Validate a normalized profile against the common schema, return plain facts.

    The single source of truth for the per-profile fact shape; ``extra="forbid"`` turns a
    drifted/typo'd key into a hard error instead of a silently-missing rubric fact.
    """
    return SocialProfileFacts(**values).as_facts()


def _summary_facts(values: JsonDict) -> JsonDict:
    return SocialSummary(**values).as_facts()


# Bio phrases that signal a call-to-action when a profile isn't a flagged business account.
_CTA_RE = re.compile(
    r"\b(call|book|booking|dm|message|quote|estimate|contact|hire|schedule|enquire|inquire|"
    r"get in touch|free consult)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
# Requires at least one letter so ranking claims like "#1" don't count as hashtags.
_HASHTAG_RE = re.compile(r"#\w*[A-Za-z]\w*")


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _first(payload: JsonDict, keys: tuple[str, ...]) -> Any:
    """First non-None value among candidate keys (provider field names vary)."""
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# A bio shorter than this (after whitespace collapse) reads as blank/placeholder rather than a
# real, descriptive profile bio — used by the substantive-bio aggregate (SAE-8).
_SUBSTANTIVE_BIO_MIN_CHARS = 20


# --- Profile-URL name parsing (shared) -------------------------------------------------------
# THE one marker-form parser: _handle_key (the scored consistency key), report._display_handle
# (every render surface), and discovery._profile_handle (brand matching) all route through
# profile_url_name, so a new platform URL shape is taught in ONE place instead of three
# hand-synced copies (the /people/ + /p/ forms had to be patched per-copy and one was missed).
_FACEBOOK_HOSTS = frozenset({"facebook.com", "fb.com"})
_YOUTUBE_HOSTS = frozenset({"youtube.com"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "instagr.am"})
# Facebook name-after-marker path forms: /pages/<slug>/<id>, /people/<Name>/<id>, legacy
# /pg/<name>, modern /p/<Name-ID>; YouTube: /c/<name>, /user/<name>. Host-scoped — 'p' on an
# Instagram URL is a post permalink, never a marker.
_FB_NAME_MARKERS = ("pages", "people", "pg", "p")
_YT_NAME_MARKERS = ("c", "user")
# Instagram first segments that are post/share permalinks or app routes — never a brand handle.
_IG_NON_PROFILE_SEGMENTS = frozenset(
    {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "directory", "about"}
)
# Facebook appends a LONG numeric page id to no-vanity name slugs
# ("Smith-Builders-104502341234567", 15+ digits today). 9+ digits so a genuine short
# digit-suffixed vanity handle ("Acme-12345") is never mistaken for an id and corrupted.
_PROFILE_ID_SUFFIX_RE = re.compile(r"-\d{9,}$")
# A marker-form path that names NO brand handle (an IG post permalink). Distinct from None
# ("no marker here, use your own vanity fallback") — falling back on one of these would key
# and display the marker word itself ("@p").
NO_PROFILE_NAME = ""


def _profile_host(hostname: Any) -> str:
    """Comparable platform host: lowercased, trailing dot stripped."""
    return str(hostname or "").lower().rstrip(".")


def _host_is(hostname: Any, bases: frozenset[str]) -> bool:
    """Registrable-host match: the host IS one of the platform's domains, or any subdomain of
    one. Facebook serves the same profile paths from www./m./web./mbasic./business. and every
    locale host (es-la., en-gb., …) — a www./m.-only prefix strip would miss those, and the
    marker walk would then fall back to the MARKER WORD ("pages") as the handle."""
    host = _profile_host(hostname)
    return any(host == base or host.endswith("." + base) for base in bases)


def profile_url_name(hostname: Any, segments: list[str]) -> str | None:
    """The brand-chosen name segment of a profile URL path. Three-valued:

    * a name — the brand segment of a marker form (``/pages/<slug>/<id>``, ``/people/<Name>/<id>``,
      legacy ``/pg/<name>``, modern ``/p/<Name-ID>``; YouTube ``/c/<name>``, ``/user/<name>``).
      The directory form ``/pages/category/<Category>/<Name-ID>/`` nests the name last, and FB's
      appended numeric page id is stripped so the name matches the brand's plain handle elsewhere.
    * ``NO_PROFILE_NAME`` ("") — the path is a recognized NON-profile path (an Instagram post/reel
      permalink or app route): there is no brand handle in it at all, and a caller must NOT fall
      back to the first segment (that would key and display the marker word, "@p").
    * ``None`` — no marker: the caller applies its own vanity-handle fallback.

    Host-aware: Facebook markers apply only on Facebook hosts, YouTube's only on YouTube —
    matching ``p``/``c``/``user`` on any platform would key an IG permalink by its shortcode.
    ONE parser for the scored consistency key, the report display, and discovery's brand
    matching, so those three can never disagree about what a stored URL's handle is."""
    lowered = [seg.lower() for seg in segments]
    if _host_is(hostname, _INSTAGRAM_HOSTS):
        return NO_PROFILE_NAME if lowered and lowered[0] in _IG_NON_PROFILE_SEGMENTS else None
    if _host_is(hostname, _FACEBOOK_HOSTS):
        markers: tuple[str, ...] = _FB_NAME_MARKERS
    elif _host_is(hostname, _YOUTUBE_HOSTS):
        markers = _YT_NAME_MARKERS
    else:
        return None
    for marker in markers:
        if marker in lowered and lowered.index(marker) + 1 < len(segments):
            index = lowered.index(marker) + 1
            if lowered[index] == "category" and index + 1 < len(segments):
                index = len(segments) - 1
            return _PROFILE_ID_SUFFIX_RE.sub("", segments[index])
    return None


def _handle_key(value: Any) -> str:
    """Normalize a handle (or full profile URL) to a comparable brand key: the vanity handle
    segment, lowercased, non-alphanumerics stripped. So ``@Acme_Studio`` / ``acmestudio`` /
    ``https://facebook.com/AcmeStudio/`` / ``https://youtube.com/@AcmeStudio`` all collapse to
    ``acmestudio`` for the consistency check. Opaque non-vanity forms (``profile.php?id=<n>``,
    ``/channel/UC…`` ids, Instagram post permalinks) return "" — they aren't brand-chosen
    handles, so they drop out of the comparison instead of false-failing it. Returns "" when
    nothing usable remains."""
    text = str(value or "").strip()
    # Scheme-less/protocol-relative links are still links (the one shared detector) — without
    # this, "www.instagram.com/acme" would be keyed as raw text ("wwwinstagramcomacme").
    link = profile_link_from_handle(text)
    if link is not None:
        text = link
    if "://" in text:
        parts = urlsplit(text)
        segments = [seg for seg in parts.path.split("/") if seg]
        if not segments:
            return ""
        lowered = [seg.lower() for seg in segments]
        if lowered[-1] == "profile.php" or "channel" in lowered:
            return ""
        name = profile_url_name(parts.hostname, segments)
        if name == NO_PROFILE_NAME and name is not None:
            # A post/share permalink names a post, not the account — drop out of the
            # comparison (the rule rescales) instead of keying on a shortcode.
            return ""
        if name is not None:
            text = name
        else:
            text = next((seg for seg in segments if seg.startswith("@")), segments[0])
    text = text.lstrip("@")
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _round_half_up(value: float, digits: int = 1) -> float:
    """Half-up rounding for non-negative values (project convention: int(x + 0.5), not round())."""
    factor = 10**digits
    return int(value * factor + 0.5) / factor


def profile_link_from_handle(handle: Any) -> str | None:
    """The handle AS a profile URL when it is one, else None — THE one URL-shaped-handle
    detector (extractor fallbacks, the YouTube channel URL, and the API's social job URL all
    route through it, so no surface can re-grow the doubled-domain bug independently).

    A URL-shaped handle is scheme'd (``https://…``), protocol-relative (``//host/…``), or
    scheme-less with a dotted host before the first slash (``www.instagram.com/acme``). A bare
    dotted HANDLE (``acme.studio`` — dots are legal in IG usernames) has no slash and is NOT a
    link."""
    cleaned = str(handle or "").strip()
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://")):
        return cleaned
    bare = cleaned.lstrip("/")
    head = bare.split("/", 1)[0]
    if "/" in bare and "." in head:
        return f"https://{bare}"
    return None


def connected_channel_matches(handle: Any, channel: JsonDict) -> bool:
    """True when the audited YouTube handle names the CONNECTED account's own channel.

    The Analytics API only ever reports on the connected account's channel (``channel==MINE``),
    so before any of its private metrics are attached to an audit the audited handle must be
    verified to BE that channel — otherwise the operator's own channel (connected once for GSC)
    would render as the client's. Conservative like the Places ``website_mismatch`` gate: an
    unresolvable comparison is a mismatch (better no connected block than a stranger's data).
    Matches either the channel id (bare ``UC…`` or a ``/channel/UC…`` link) or the vanity
    ``@handle`` (``customUrl``), compared via the same key the consistency check uses."""
    if not isinstance(channel, dict):
        return False
    text = str(handle or "").strip()
    if not text:
        return False
    channel_id = str(channel.get("id") or "").strip()
    probe = profile_link_from_handle(text) or text
    if channel_id and channel_id in probe:
        return True
    handle_key = _handle_key(text)
    custom_key = _handle_key(str(channel.get("custom_url") or ""))
    return bool(handle_key) and handle_key == custom_key


def _fallback_profile_url(handle: str, platform_base: str) -> str:
    """Canonical profile URL derived from the audited handle, for providers that returned no URL.

    Auto-discovery (and an operator pasting a link) supplies the handle AS a full profile URL —
    pass that through verbatim instead of nesting it under the platform host, which would mint
    a doubled-domain URL like ``https://www.instagram.com/https://www.instagram.com/acme//``."""
    link = profile_link_from_handle(handle)
    if link is not None:
        return link
    return f"{platform_base}/{handle.strip().lstrip('@').strip('/')}/"


# The one canonical average-month length (365.25/12). google_search_console imports this for
# its monthly normalizer, so "per month" means the same thing for social cadence and search
# figures inside one report.
AVG_DAYS_PER_MONTH = 30.44


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _posts_per_month(times: list[datetime]) -> float | None:
    if len(times) < 2:
        return None
    span_days = (times[-1] - times[0]).total_seconds() / 86400
    if span_days < 1:
        return float(len(times))
    return _round_half_up(len(times) / span_days * AVG_DAYS_PER_MONTH)


def _avg_engagement(posts: list[JsonDict], followers: int) -> float | None:
    if followers <= 0:
        return None
    rates: list[float] = []
    for post in posts:
        likes_raw = post.get("likesCount")
        comments_raw = post.get("commentsCount")
        # Both absent => engagement is hidden/unknown for this post (e.g. a YouTube video
        # with likes AND comments hidden). Skip it rather than scoring it as zero, so the
        # rate stays None and the engagement rule skip_if_missing-rescales instead of failing.
        if likes_raw is None and comments_raw is None:
            continue
        likes = max(_int(likes_raw), 0)
        comments = max(_int(comments_raw), 0)
        rates.append((likes + comments) / followers * 100)
    return _mean(rates, 2)


def _has_video(raw: JsonDict, posts: list[JsonDict]) -> bool:
    if _int(raw.get("igtvVideoCount")) > 0:
        return True
    # Routed through _post_kind so the "is this a video?" predicate can't drift from the mix.
    return any(_post_kind(post) == "video" for post in posts)


def _short_date(value: Any) -> str | None:
    ts = _parse_ts(value)
    return ts.strftime("%b %d, %Y") if ts else None


def _post_kind(post: JsonDict) -> str:
    """video | carousel | image | unknown for a normalized post, from the provider ``type`` only.

    View counts are deliberately not a fallback signal — a generic reach field on a non-video
    post must not classify it as video. Untyped posts (e.g. Facebook text/link/status posts
    mapped to type ``post``) are ``unknown``: the payload can't tell image from carousel.
    """
    kind = str(post.get("type", "")).lower()
    if kind == "video":
        return "video"
    if kind in {"sidecar", "carousel", "carousel_album"}:
        return "carousel"
    if kind in {"image", "photo"}:
        return "image"
    return "unknown"


def _content_mix(posts: list[JsonDict]) -> tuple[float | None, float | None, float | None]:
    """(video%, image%, carousel%) of the sampled posts, or (None, None, None) when no posts.

    Video share is always video/n — text posts legitimately dilute it. When any post kind is
    unknown, image and carousel shares are None (the payload can't tell those kinds apart, and
    claiming "Image 100%" for a text-only page would be dishonest).
    """
    if not posts:
        return (None, None, None)
    n = len(posts)
    kinds = [_post_kind(p) for p in posts]
    video_pct = _round_half_up(kinds.count("video") / n * 100)
    if "unknown" in kinds:
        return (video_pct, None, None)
    return (
        video_pct,
        _round_half_up(kinds.count("image") / n * 100),
        _round_half_up(kinds.count("carousel") / n * 100),
    )


def _max_posting_gap_days(times: list[datetime], now: datetime) -> int | None:
    """Longest gap (days) between consecutive sampled posts OR since the last post.

    Counting the trailing gap (last post -> now) is load-bearing: otherwise a dormant account
    whose few sampled posts happen to be close together passes the consistency rule and gets
    praised as "posting regularly" in the same report that flags it stale (see test).
    """
    if not times:
        return None
    gaps = [(times[i + 1] - times[i]).days for i in range(len(times) - 1)]
    # Clamped at 0: a future-dated post (scheduled post / clock skew) must not yield a
    # negative trailing gap.
    gaps.append(max(0, (now - times[-1]).days))
    return max(gaps)


def _days_since_last_post(times: list[datetime], now: datetime) -> int | None:
    # Clamped at 0: a future-dated post (scheduled post / clock skew) must not yield "-1 days".
    return max(0, (now - times[-1]).days) if times else None


def _like_to_comment_ratio(posts: list[JsonDict]) -> float | None:
    likes = sum(max(_int(p.get("likesCount")), 0) for p in posts if p.get("likesCount") is not None)
    comments = sum(
        max(_int(p.get("commentsCount")), 0) for p in posts if p.get("commentsCount") is not None
    )
    return _round_half_up(likes / comments) if comments > 0 else None


def _avg_views_per_post(posts: list[JsonDict]) -> float | None:
    """Average views per video post (views-tracked posts only); a real 0-view video counts."""
    views = [_int(p.get("videoViewCount")) for p in posts if p.get("videoViewCount") is not None]
    return _mean(views)


def _best_post_engagement(posts: list[JsonDict]) -> int | None:
    totals = [
        max(_int(p.get("likesCount")), 0) + max(_int(p.get("commentsCount")), 0)
        for p in posts
        if p.get("likesCount") is not None or p.get("commentsCount") is not None
    ]
    return max(totals) if totals else None


def _avg_hashtags_per_post(posts: list[JsonDict]) -> float | None:
    """Mean hashtags/post from an explicit ``hashtags`` list or, failing that, the caption text.
    None when no post has hashtag/caption data (so the hashtag rule skip_if_missing-rescales)."""
    counts: list[int] = []
    for post in posts:
        tags = post.get("hashtags")
        if isinstance(tags, list):
            counts.append(len([t for t in tags if t]))
        elif isinstance(post.get("caption"), str):
            counts.append(len(_HASHTAG_RE.findall(post["caption"])))
    return _mean(counts)


def _caption_cta_pct(posts: list[JsonDict]) -> float | None:
    captions = [
        p["caption"] for p in posts if isinstance(p.get("caption"), str) and p["caption"].strip()
    ]
    if not captions:
        return None
    hits = sum(1 for caption in captions if _CTA_RE.search(caption))
    return _round_half_up(hits / len(captions) * 100)


def _top_posts(posts: list[JsonDict], *, limit: int = 3) -> list[JsonDict]:
    """Best sampled posts by combined attention (views + engagement) — the 'top performing
    content' table. ``views`` is emitted only for posts that carried a view count, so a real
    0-view video renders as 0 while image posts stay None."""
    enriched: list[JsonDict] = []
    for post in posts:
        likes = max(_int(post.get("likesCount")), 0)
        comments = max(_int(post.get("commentsCount")), 0)
        views_raw = post.get("videoViewCount")
        views = _int(views_raw) if views_raw is not None else None
        if post.get("likesCount") is None and post.get("commentsCount") is None and views is None:
            continue
        enriched.append(
            {
                "type": _post_kind(post),
                "views": views,
                "likes": likes,
                "comments": comments,
                "engagement": likes + comments,
                "posted": _short_date(post.get("timestamp")),
                "title": _clean(post.get("title")) or None,
            }
        )
    # A combined proxy so a barely-watched video can't outrank a heavily-liked image.
    enriched.sort(key=lambda p: (p["views"] or 0) + p["engagement"], reverse=True)
    return enriched[:limit]


def _post_detail_facts(posts: list[JsonDict], times: list[datetime], *, now: datetime) -> JsonDict:
    """Per-profile content/performance detail derived from the sampled posts (all None-safe).

    ``times`` is the already-parsed/sorted post timestamps the caller computed (reused here so the
    cadence facts and the posting-gap fact never derive from two separate parses that could drift).
    """
    video_pct, image_pct, carousel_pct = _content_mix(posts)
    return {
        "video_share_pct": video_pct,
        "image_share_pct": image_pct,
        "carousel_share_pct": carousel_pct,
        "max_posting_gap_days": _max_posting_gap_days(times, now),
        "like_to_comment_ratio": _like_to_comment_ratio(posts),
        "avg_views_per_post": _avg_views_per_post(posts),
        "best_post_engagement": _best_post_engagement(posts),
        "avg_hashtags_per_post": _avg_hashtags_per_post(posts),
        "posts_with_cta_caption_pct": _caption_cta_pct(posts),
        "top_posts": _top_posts(posts),
    }


def normalize_instagram_profile(raw: JsonDict, handle: str, *, now: datetime) -> JsonDict:
    bio = _clean(raw.get("biography"))
    external = _clean(raw.get("externalUrl"))
    full_name = _clean(raw.get("fullName"))
    followers = _int(raw.get("followersCount"))
    business_flag = raw.get("isBusinessAccount")
    # Missing from the payload => the scraper didn't report the setting — unknown (None), NOT a
    # personal account; a hard False here would surface a false client-facing finding.
    is_business = None if business_flag is None else bool(business_flag)
    follows = _int(raw.get("followsCount"))
    posts = raw.get("latestPosts")
    posts = [p for p in posts if isinstance(p, dict)] if isinstance(posts, list) else []
    times = sorted(t for t in (_parse_ts(p.get("timestamp")) for p in posts) if t)

    return _profile_facts(
        {
            "platform": "instagram",
            "handle": handle,
            "url": _clean(raw.get("url"))
            or _fallback_profile_url(handle, "https://www.instagram.com"),
            "status": "complete",
            "followers": followers,
            "posts_count": _int(raw.get("postsCount")),
            "verified": bool(raw.get("verified")),
            "private": bool(raw.get("private")),
            "is_business": is_business,
            "category": _clean(raw.get("businessCategoryName")) or None,
            "bio_present": bool(bio),
            "bio_text": bio or None,
            # The official Instagram scraper returns no public phone/address (verified) -> None,
            # so the NAP cross-check leans on Facebook/Places and skips here.
            "link_in_bio": external or None,
            "has_cta": bool(is_business) or bool(_CTA_RE.search(bio)),
            "profile_complete": bool(bio and external and full_name),
            "has_logo_avatar": bool(raw.get("profilePicUrl") or raw.get("profilePicUrlHD")),
            "posts_sampled": len(posts),
            "posts_per_month": _posts_per_month(times),
            "days_since_last_post": _days_since_last_post(times, now),
            "avg_engagement_rate_pct": _avg_engagement(posts, followers),
            "has_video": _has_video(raw, posts),
            "follows_count": follows,
            "follower_following_ratio": (
                _round_half_up(followers / follows) if follows > 0 else None
            ),
            "total_views": None,
            **_post_detail_facts(posts, times, now=now),
        }
    )


def normalize_facebook_profile(raw: JsonDict, handle: str, *, now: datetime) -> JsonDict:
    # Page metadata comes from the Pages actor. Posts (when present) are merged in by the
    # collector from the Facebook Posts actor; without them, cadence/recency/engagement stay
    # None and the corresponding social.yaml rules skip_if_missing (rescale, never penalize).
    intro = _clean(raw.get("intro")) or _clean(raw.get("about"))
    website = _clean(raw.get("website"))
    name = _clean(raw.get("pageName")) or _clean(raw.get("title"))
    followers = _int(raw.get("followers")) or _int(raw.get("likes"))
    messenger = _clean(raw.get("messenger"))
    email = _clean(raw.get("email"))
    # The Facebook Pages actor DOES return phone/address, but only when the Page exposes them
    # publicly (fill-rate is a per-page unknown — SAE-2); None otherwise so NAP skips.
    phone = _clean(raw.get("phone")) or _clean(raw.get("phoneNumber"))
    address = _clean(raw.get("address")) or _clean(raw.get("addressStreet"))

    raw_posts = [p for p in (raw.get("posts") or []) if isinstance(p, dict)]
    posts: list[JsonDict] = []
    for p in raw_posts:
        # viewsCount is a generic reach field on the Posts actor; mapping it into
        # videoViewCount unconditionally would make a text post look like a video, so view
        # counts are kept only for actual videos. Non-video posts stay type "post" (=> kind
        # "unknown"): the payload can't distinguish an image post from a carousel.
        is_video = bool(p.get("video") or p.get("videoUrl") or p.get("isVideo"))
        posts.append(
            {
                "likesCount": _first(p, ("likes", "likesCount", "reactionsCount", "reactions")),
                "commentsCount": _first(p, ("comments", "commentsCount")),
                "videoViewCount": (
                    _first(p, ("viewsCount", "videoViewCount", "videoViews")) if is_video else None
                ),
                "timestamp": _first(p, ("time", "timestamp", "date", "publishedAt", "postedAt")),
                "type": "video" if is_video else "post",
                "caption": _first(p, ("text", "message", "caption")),
            }
        )
    times = sorted(t for t in (_parse_ts(p.get("timestamp")) for p in posts) if t)

    return _profile_facts(
        {
            "platform": "facebook",
            "handle": handle,
            "url": _clean(raw.get("facebookUrl"))
            or _clean(raw.get("pageUrl"))
            or _fallback_profile_url(handle, "https://www.facebook.com"),
            "status": "complete",
            "followers": followers,
            "posts_count": len(raw_posts),
            "verified": bool(raw.get("verified")),
            "private": False,
            # A Facebook *Page* — the unit the Pages actor scrapes — is a business presence by
            # definition (personal profiles aren't Pages), so True is a fact of the fetched
            # object, not a guess. IG reads the account's own flag; YouTube has no such concept
            # (None). Deliberate: don't "fix" this to a raw-payload lookup that doesn't exist.
            "is_business": True,
            "category": _clean(raw.get("category")) or None,
            "bio_present": bool(intro),
            "bio_text": intro or None,
            "phone": phone or None,
            "address": address or None,
            "link_in_bio": website or None,
            "has_cta": bool(messenger or email),
            "profile_complete": bool(intro and website and name),
            "has_logo_avatar": bool(raw.get("profilePhoto") or raw.get("profilePictureUrl")),
            "posts_sampled": len(posts),
            "posts_per_month": _posts_per_month(times),
            "days_since_last_post": _days_since_last_post(times, now),
            "avg_engagement_rate_pct": _avg_engagement(posts, followers),
            "has_video": _has_video(raw, posts),
            "follows_count": 0,
            "follower_following_ratio": None,
            "total_views": None,
            **_post_detail_facts(posts, times, now=now),
        }
    )


def normalize_youtube_channel(raw: JsonDict, handle: str, *, now: datetime) -> JsonDict:
    # YouTube Data API v3 payload: {"channel": <channels item>, "videos": [<videos items>]}.
    # Recent uploads are mapped into the same post shape IG uses so the cadence/recency/
    # engagement helpers are reused. subscriberCount can be hidden -> 0 (rules skip/rescale).
    channel = raw.get("channel") if isinstance(raw.get("channel"), dict) else {}
    snippet = channel.get("snippet") if isinstance(channel.get("snippet"), dict) else {}
    stats = channel.get("statistics") if isinstance(channel.get("statistics"), dict) else {}
    branding = (
        channel.get("brandingSettings") if isinstance(channel.get("brandingSettings"), dict) else {}
    )
    videos = [v for v in (raw.get("videos") or []) if isinstance(v, dict)]

    description = _clean(snippet.get("description"))
    title = _clean(snippet.get("title"))
    link_match = _URL_RE.search(description)
    link = link_match.group(0).rstrip(".,);") if link_match else ""
    banner = _clean((branding.get("image") or {}).get("bannerExternalUrl"))
    followers = _int(stats.get("subscriberCount"))
    video_count = _int(stats.get("videoCount"))
    lifetime_views = _int(stats.get("viewCount"))
    custom_url = _clean(snippet.get("customUrl")).lstrip("@").strip("/")

    posts = [
        {
            "likesCount": (v.get("statistics") or {}).get("likeCount"),
            "commentsCount": (v.get("statistics") or {}).get("commentCount"),
            "videoViewCount": (v.get("statistics") or {}).get("viewCount"),
            "timestamp": (v.get("snippet") or {}).get("publishedAt"),
            "type": "video",
            "title": (v.get("snippet") or {}).get("title"),
        }
        for v in videos
    ]
    times = sorted(t for t in (_parse_ts(p.get("timestamp")) for p in posts) if t)

    handle_link = profile_link_from_handle(handle)
    if custom_url:
        channel_url = f"https://www.youtube.com/@{custom_url}"
    elif handle_link is not None:
        # Auto-discovery (or a pasted link) supplies the handle AS a full channel URL — keep it
        # verbatim rather than nesting it under /@… (which would mint a doubled-domain URL).
        channel_url = handle_link
    else:
        bare_handle = handle.strip().lstrip("@").strip("/")
        if bare_handle.startswith("UC") and len(bare_handle) == 24:
            # Channel IDs use the /channel/UC… form, not /@…
            channel_url = f"https://www.youtube.com/channel/{bare_handle}"
        else:
            channel_url = f"https://www.youtube.com/@{bare_handle}"

    return _profile_facts(
        {
            "platform": "youtube",
            "handle": handle,
            "url": channel_url,
            "status": "complete",
            "followers": followers,
            "posts_count": video_count,
            "verified": False,
            "private": False,
            # YouTube has no Business/Creator-account concept -> None (not False) so the
            # business-account rule skip_if_missing-rescales instead of vacuously failing it.
            "is_business": None,
            "category": None,
            "bio_present": bool(description),
            "bio_text": description or None,
            "link_in_bio": link or None,
            "has_cta": bool(link) or bool(_CTA_RE.search(description)),
            "profile_complete": bool(description and title and (link or banner)),
            "has_logo_avatar": bool(snippet.get("thumbnails")),
            "posts_sampled": len(posts),
            "posts_per_month": _posts_per_month(times),
            "days_since_last_post": _days_since_last_post(times, now),
            "avg_engagement_rate_pct": _avg_engagement(posts, followers),
            "has_video": bool(videos) or video_count > 0,
            "follows_count": 0,
            "follower_following_ratio": None,
            "total_views": lifetime_views or None,
            **_post_detail_facts(posts, times, now=now),
        }
    )


def _empty_summary() -> JsonDict:
    # The schema's defaults ARE the empty-audit summary (single source of truth).
    return SocialSummary().as_facts()


def _collect(profiles: list[JsonDict], key: str) -> list[Any]:
    """Non-None values of ``key`` across profiles (skips platforms lacking that fact)."""
    return [p[key] for p in profiles if p.get(key) is not None]


def _mean(values: list[Any], digits: int = 1) -> float | None:
    return _round_half_up(sum(values) / len(values), digits) if values else None


def summarize_profiles(profiles: list[JsonDict]) -> JsonDict:
    count = len(profiles)
    if count == 0:
        return _empty_summary()
    ppm = _collect(profiles, "posts_per_month")
    dsp = _collect(profiles, "days_since_last_post")
    eng = _collect(profiles, "avg_engagement_rate_pct")
    gaps = _collect(profiles, "max_posting_gap_days")
    total_views = _collect(profiles, "total_views")
    # Only profiles on a platform that HAS a business-account concept report is_business (IG/FB
    # a real bool, YouTube None). None when none of them do, so the rule skip_if_missing-rescales
    # rather than penalizing e.g. a YouTube-only audit for a setting YouTube doesn't have.
    business = _collect(profiles, "is_business")
    # YouTube uploads are definitionally 100% video, so a channel in the mix would make the
    # scored video-share rule vacuous (and swamp the image/carousel shares). Content-mix shares
    # aggregate over non-YouTube profiles only; None when none supplies a value (rule rescales).
    feed_profiles = [p for p in profiles if p.get("platform") != "youtube"]
    # Handle consistency (SAE-7): compare the normalized brand key across profiles. Needs >= 2
    # comparable handles; None otherwise so the boolean rule skip_if_missing-rescales.
    handle_keys = [k for k in (_handle_key(p.get("handle")) for p in profiles) if k]
    handles_consistent = (len(set(handle_keys)) == 1) if len(handle_keys) >= 2 else None
    # Category coverage (SAE-9) over feed platforms only — YouTube has no business-category concept,
    # so a YouTube-only audit yields None and the rule skips instead of false-failing.
    categorized_feed = sum(1 for p in feed_profiles if p.get("category"))
    category_coverage_pct = (
        _round_half_up(categorized_feed / len(feed_profiles) * 100) if feed_profiles else None
    )
    return _summary_facts(
        {
            "platforms_audited": count,
            "total_followers": sum(_int(p.get("followers")) for p in profiles),
            "profiles_complete_pct": int(
                sum(1 for p in profiles if p.get("profile_complete")) / count * 100 + 0.5
            ),
            # None (not 0) when no profile has post data, so the cadence/recency/engagement
            # rules skip_if_missing and rescale out instead of unfairly failing (e.g. the
            # Facebook pages actor returns no posts).
            "avg_posts_per_month": _mean(ppm),
            "days_since_last_post": min(dsp) if dsp else None,
            "avg_engagement_rate_pct": _mean(eng, 2),
            "profiles_with_link_in_bio": sum(1 for p in profiles if p.get("link_in_bio")),
            "profiles_with_cta": sum(1 for p in profiles if p.get("has_cta")),
            "has_video_content": any(p.get("has_video") for p in profiles),
            "profiles_with_logo_avatar": sum(1 for p in profiles if p.get("has_logo_avatar")),
            # Extended detail aggregates (some scored, the rest surfaced as content insights).
            "profiles_verified": sum(1 for p in profiles if p.get("verified")),
            "profiles_business_account": sum(1 for v in business if v) if business else None,
            "profiles_with_category": sum(1 for p in profiles if p.get("category")),
            "handles_consistent": handles_consistent,
            "profiles_with_substantive_bio": sum(
                1
                for p in profiles
                if len((p.get("bio_text") or "").strip()) >= _SUBSTANTIVE_BIO_MIN_CHARS
            ),
            "category_coverage_pct": category_coverage_pct,
            "avg_follower_following_ratio": _mean(_collect(profiles, "follower_following_ratio")),
            "video_share_pct": _mean(_collect(feed_profiles, "video_share_pct")),
            "image_share_pct": _mean(_collect(feed_profiles, "image_share_pct")),
            "carousel_share_pct": _mean(_collect(feed_profiles, "carousel_share_pct")),
            "max_posting_gap_days": max(gaps) if gaps else None,
            "avg_views_per_post": _mean(_collect(profiles, "avg_views_per_post")),
            "total_views": sum(_int(v) for v in total_views) if total_views else None,
            "avg_like_to_comment_ratio": _mean(_collect(profiles, "like_to_comment_ratio")),
            "avg_hashtags_per_post": _mean(_collect(profiles, "avg_hashtags_per_post")),
            "posts_with_cta_caption_pct": _mean(_collect(profiles, "posts_with_cta_caption_pct")),
        }
    )


def extract_social_facts(fetched: list[JsonDict], *, now: datetime | None = None) -> JsonDict:
    """Normalize a list of fetched provider payloads into the social fact bundle.

    Each entry is ``{"platform", "handle", "raw"}`` where ``raw`` is the provider payload
    (or ``None`` when that fetch failed). Status follows the external-source vocabulary:
    skipped (nothing fetched) / failed (all fetches failed) / partial / complete.
    """
    now = now or datetime.now(UTC)
    if not fetched:
        return {
            "status": "skipped",
            "source": "social",
            "summary": _empty_summary(),
            "platforms": [],
        }

    platforms: list[JsonDict] = []
    for entry in fetched:
        raw = entry.get("raw")
        platform = entry.get("platform", "instagram")
        handle = entry.get("handle", "")
        if not raw:
            platforms.append({"platform": platform, "handle": handle, "status": "failed"})
        elif platform == "instagram":
            platforms.append(normalize_instagram_profile(raw, handle, now=now))
        elif platform == "facebook":
            platforms.append(normalize_facebook_profile(raw, handle, now=now))
        elif platform == "youtube":
            platforms.append(normalize_youtube_channel(raw, handle, now=now))
        else:
            # Unknown platform — don't penalize the score.
            platforms.append({"platform": platform, "handle": handle, "status": "unsupported"})

    complete = [p for p in platforms if p.get("status") == "complete"]
    attempted = [p for p in platforms if p.get("status") in {"complete", "failed"}]
    if not complete:
        status = "failed" if any(p.get("status") == "failed" for p in platforms) else "skipped"
    elif len(complete) < len(attempted):
        status = "partial"
    else:
        status = "complete"

    summary = summarize_profiles(complete) if complete else _empty_summary()
    return {"status": status, "source": "social", "summary": summary, "platforms": platforms}


# --- Combined-audit business-identity context (SAE-9/10/12/13) ------------------------------
# These run AFTER collection, from the worker: they need website-side context (the audited
# site's phones, the operator's niche, the verified Google listing) that the collector never
# sees. They live here — not in tasks.py — so fact production stays in the pure extractor and
# every write is re-validated through the SocialSummary schema (``extra="forbid"``): a drifted
# or typo'd fact key is a hard error, never a silently-skipped rule.


def _phone_tail(value: Any) -> str:
    """Last 10 digits of a phone string (US-style comparison key); "" if fewer than 10 digits."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def _revalidate_summary(social_facts: JsonDict) -> None:
    summary = social_facts.get("summary")
    if isinstance(summary, dict):
        social_facts["summary"] = _summary_facts(summary)


def inject_google_business(social_facts: JsonDict, google_business: JsonDict | None) -> None:
    """Attach the VERIFIED Google listing (see places_provider's website gate) plus its scored
    summary signals. ``None``/non-dict listing => no mutation (rules skip-rescale)."""
    summary = social_facts.get("summary")
    if not isinstance(summary, dict) or not isinstance(google_business, dict):
        return
    social_facts["google_business"] = google_business
    summary["google_rating"] = google_business.get("rating")
    summary["google_review_count"] = google_business.get("review_count")
    _revalidate_summary(social_facts)


def inject_category_relevance(social_facts: JsonDict, *, niche: str | None) -> None:
    """SAE-9-full: ``summary.category_matches_niche`` from the audit's niche and the declared
    categories (feed profiles + the Google listing). ``None`` (rule skips) when the niche can't
    be classified or no category is set, so a vague niche never yields a false 'wrong category'
    finding."""
    summary = social_facts.get("summary")
    if not isinstance(summary, dict):
        return
    categories = [
        p.get("category")
        for p in social_facts.get("platforms") or []
        if isinstance(p, dict) and p.get("platform") != "youtube"
    ]
    gbp = social_facts.get("google_business")
    if isinstance(gbp, dict) and gbp.get("category"):
        categories.append(gbp.get("category"))
    summary["category_matches_niche"] = category_relevance(niche, categories)
    _revalidate_summary(social_facts)


def inject_nap_consistency(social_facts: JsonDict, *, website_phone_keys: set[str]) -> None:
    """SAE-10/13 tri-way NAP: ``summary.nap_phone_consistent`` compares the website's phone
    key(s) with the business's (social profiles + Google listing). Left ``None`` (rule
    ``skip_if_missing``-rescales) whenever either side has no comparable phone — why a
    standalone social audit, or a profile that hides its number, never false-fails. Forgiving
    by design: consistent when the website shares ANY number with a business source (a site
    that also lists a fax/second line never false-fails)."""
    summary = social_facts.get("summary")
    if not isinstance(summary, dict):
        return
    business = {
        key
        for p in social_facts.get("platforms") or []
        if isinstance(p, dict) and (key := _phone_tail(p.get("phone")))
    }
    gbp = social_facts.get("google_business")
    if isinstance(gbp, dict) and (key := _phone_tail(gbp.get("phone"))):
        business.add(key)
    if not website_phone_keys or not business:
        return
    summary["nap_phone_consistent"] = bool(website_phone_keys & business)
    _revalidate_summary(social_facts)
