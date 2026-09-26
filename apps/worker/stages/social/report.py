"""Compose the standalone Social report payload from a stored social audit result.

Pure function (no rendering/IO) shared by the PDF renderer and the API detail response —
mirrors how report_payload.compose_report_payload serves the website audit. Findings and
the tiered roadmap come straight from the social rubric's rule metadata (deterministic; no
LLM), so the report is reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from apps.worker.stages.social.extractor import profile_link_from_handle, profile_url_name

JsonDict = dict[str, Any]
SOCIAL_REPORT_VERSION = "phase2-social-report-v1"
_TIERS = ("quick_win", "mid_term", "long_term")


def _dict(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _fmt(value: Any) -> str:
    """Compact number formatting: drop a trailing .0, keep one decimal otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _unit(fact_path: str) -> str:
    """Legacy fact-path heuristic — the fallback for stored breakdowns that predate the
    rubric's declared per-rule ``unit`` metadata (pre-social-v6 runs)."""
    if fact_path.endswith("_pct") or "engagement" in fact_path:
        return "%"
    if "days" in fact_path:
        return " days"
    return ""


def _metric_line(rule: JsonDict) -> str | None:
    """A quantified 'measured X vs target Y' line for a finding, from the rule's own evidence —
    so the report says '~1.3 posts/month (target ≥ 8)' instead of a bare 'Infrequent posting'."""
    evidence = _dict(rule.get("evidence"))
    value = evidence.get("value")
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Prefer the unit the rubric declares next to the rule (social.yaml `unit:`); fall back to
    # the fact-path heuristic for breakdowns stored before units were declared.
    declared = rule.get("unit")
    unit = declared if isinstance(declared, str) else _unit(str(rule.get("fact_path", "")))
    params = _dict(evidence.get("params"))
    targets: list[str] = []
    if params.get("min") is not None:
        targets.append(f"≥ {_fmt(params['min'])}{unit}")
    if params.get("max") is not None:
        targets.append(f"≤ {_fmt(params['max'])}{unit}")
    measured = f"{_fmt(value)}{unit}"
    return f"{measured} (target {' / '.join(targets)})" if targets else measured


def _google_rating_line(google_business: JsonDict) -> str | None:
    """One precomposed rating sentence for the Google Business block — single-sourced so the
    PDF and DOCX copy can never drift ("Rated 4.7/5 from 128 Google reviews")."""
    rating = google_business.get("rating")
    count = google_business.get("review_count")
    if rating is not None and count is not None:
        plural = "s" if count != 1 else ""
        return f"Rated {rating}/5 from {count} Google review{plural}"
    if rating is not None:
        return f"Rated {rating}/5 on Google"
    if count is not None:
        plural = "s" if count != 1 else ""
        return f"{count} Google review{plural}"
    return None


def _display_handle(value: Any) -> str:
    """Human-readable handle from a stored handle-or-profile-URL.

    Auto-discovered handles are full canonical profile URLs, so without this the report would
    show '@https://www.instagram.com/acme/' instead of '@acme'. Scheme-less links are links
    too (profile_link_from_handle — the one shared detector), and the marker-form name walk
    is the shared, host-aware profile_url_name (one parser for the scored consistency key,
    discovery's brand matching, and this display — no per-copy drift)."""
    text = str(value or "").strip()
    link = profile_link_from_handle(text)
    if link is not None:
        text = link
    if "://" not in text:
        return text.lstrip("@")
    parsed = urlsplit(text)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return text
    # facebook.com/profile.php?id=<n> has no vanity segment — show the numeric id.
    if segments[-1].lower() == "profile.php":
        profile_id = parse_qs(parsed.query).get("id", [""])[0]
        return f"id {profile_id}" if profile_id else segments[-1]
    # facebook.com/pages|people|pg|p/… and youtube.com/c|user/… name-after-marker forms; "" when
    # the URL names no handle at all (an IG post permalink) — the surfaces then render the
    # empty-handle placeholder rather than the marker word ("@p").
    name = profile_url_name(parsed.hostname, segments)
    if name is not None:
        return name
    lowered = [segment.lower() for segment in segments]
    # youtube.com/channel/UC… has no vanity name — show the channel id.
    if "channel" in lowered and lowered.index("channel") + 1 < len(segments):
        return segments[lowered.index("channel") + 1]
    # youtube.com/@handle (any position), else the IG/FB vanity first segment.
    for segment in segments:
        if segment.startswith("@"):
            return segment.lstrip("@")
    return segments[0].lstrip("@")


def _strength_label(rule: JsonDict) -> str:
    """A positive one-liner for a passing check (strip the parenthetical from the description)."""
    text = str(rule.get("description") or rule.get("finding_label") or rule.get("rule_id") or "")
    return text.split("(")[0].strip().rstrip(".") or text


def _connected_youtube_block(facts: JsonDict) -> JsonDict | None:
    """Owner-consent YouTube Analytics block (SAE-15/SMWA-140), or None when not collected.

    The display LINES are precomposed HERE — the shared builder — so the PDF, DOCX, and web UI
    render byte-identical prose (the same no-drift pattern as ``rating_line``)."""
    data = _dict(facts.get("youtube_analytics"))
    if not data:
        return None
    lines: list[str] = []
    if data.get("views") is not None:
        lines.append(f"Views: {_fmt(data['views'])}")
    if data.get("estimated_minutes_watched") is not None:
        lines.append(f"Watch time: {_fmt(data['estimated_minutes_watched'])} minutes")
    if data.get("avg_view_duration_seconds") is not None:
        duration = f"Average view duration: {_fmt(data['avg_view_duration_seconds'])}s"
        if data.get("avg_view_percentage") is not None:
            duration += f" ({_fmt(data['avg_view_percentage'])}% of video watched)"
        lines.append(duration)
    if data.get("subscribers_gained") is not None or data.get("subscribers_lost") is not None:
        gained = _fmt(data.get("subscribers_gained") or 0)
        lost = _fmt(data.get("subscribers_lost") or 0)
        lines.append(f"Subscribers: +{gained} / -{lost}")
    sources = [
        s
        for s in (data.get("traffic_sources") or [])
        if isinstance(s, dict) and s.get("source") and s.get("views") is not None
    ][:3]
    if sources:
        lines.append(
            "Top traffic sources: "
            + ", ".join(f"{s['source']} ({_fmt(s['views'])} views)" for s in sources)
        )
    if not lines:
        # Nothing renderable — hide the section rather than emit a dangling heading.
        return None
    window = _dict(data.get("window"))
    meta = "Owner-consent YouTube Analytics"
    # Name the verified channel so the reader can SEE which channel these private metrics
    # belong to (the worker only attaches metrics after the identity check passes).
    channel = _dict(data.get("channel"))
    channel_label = channel.get("handle") or channel.get("title")
    if channel_label:
        meta += f" for {channel_label}"
    if window.get("start_date") and window.get("end_date"):
        meta += f" · {window['start_date']} to {window['end_date']}"
    return {"meta": meta, "lines": lines}


def build_social_report_data(
    *,
    social_facts: Any,
    social_breakdown: Any,
    social_score: Any,
    handles: Any,
    commentary: Any = None,
) -> JsonDict:
    """Pure builder of the social report dict from its raw pieces.

    Shared by the standalone social audit (``compose_social_report_payload``) and the combined
    audit's appended social section (``report_payload``), so both derive findings/roadmap from the
    same deterministic rule metadata. ``commentary`` is the optional LLM-polished envelope (only
    the standalone social pipeline sets it); pass ``None`` for the deterministic baseline."""
    facts = _dict(social_facts)
    breakdown = _dict(social_breakdown)
    summary = _dict(facts.get("summary"))
    # Public Google Business Profile block (SAE-13), present only on a combined audit with a Places
    # key + a matched listing; None otherwise so consumers hide the section (no dangling heading).
    # rating_line is precomposed HERE (the shared builder) so the PDF and DOCX prose can't drift.
    google_business = _dict(facts.get("google_business")) or None
    if google_business:
        google_business = {
            **google_business,
            "rating_line": _google_rating_line(google_business),
        }
    # Connected-mode YouTube Analytics (flag-gated OFF by default); None ⇒ section hidden.
    connected_youtube = _connected_youtube_block(facts)
    category = _dict(breakdown.get("category"))
    rules = category.get("rules") if isinstance(category.get("rules"), list) else []

    # Shallow copies so the display-handle rewrite never mutates the stored facts.
    platforms = [
        {**p, "handle": _display_handle(p.get("handle"))} if p.get("handle") is not None else {**p}
        for p in (facts.get("platforms") or [])
        if isinstance(p, dict) and p.get("status") == "complete"
    ]

    # Optional LLM-polished prose. When present, attach the executive summary and a per-finding
    # narrative; otherwise the report is the pure rule-derived deterministic output.
    commentary = _dict(commentary)
    content = _dict(commentary.get("content"))
    narratives = {
        f.get("id"): f.get("narrative")
        for f in (content.get("findings") or [])
        if isinstance(f, dict) and f.get("narrative")
    }

    findings: list[JsonDict] = []
    strengths: list[JsonDict] = []
    roadmap: dict[str, list[JsonDict]] = {tier: [] for tier in _TIERS}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if not rule.get("surface_as_finding", True):
            continue
        rule_id = rule.get("rule_id")
        if rule.get("result") == "pass":
            # "What's working" — so a strong account gets a substantive report, not an empty one.
            strengths.append({"id": rule_id, "label": _strength_label(rule)})
            continue
        if rule.get("result") not in {"fail", "partial"}:
            continue
        item = {
            "id": rule_id,
            "label": rule.get("finding_label") or rule.get("description") or rule_id,
            "metric": _metric_line(rule),
            "remediation": rule.get("remediation"),
            "impact": rule.get("impact") or "medium",
            "tier": rule.get("tier") or "quick_win",
            "result": rule.get("result"),
            "narrative": narratives.get(rule_id) or "",
        }
        findings.append(item)
        roadmap.get(item["tier"], roadmap["quick_win"]).append(item)

    # Content & performance insights — surfaced from facts the extractor derives but the rubric
    # doesn't score (content mix, views, engagement shape), so the report says specific things.
    content_insights = {
        "content_mix": {
            "video": summary.get("video_share_pct"),
            "image": summary.get("image_share_pct"),
            "carousel": summary.get("carousel_share_pct"),
        },
        "total_views": summary.get("total_views"),
        "avg_views_per_post": summary.get("avg_views_per_post"),
        "avg_engagement_rate_pct": summary.get("avg_engagement_rate_pct"),
        "avg_like_to_comment_ratio": summary.get("avg_like_to_comment_ratio"),
        "max_posting_gap_days": summary.get("max_posting_gap_days"),
        "avg_hashtags_per_post": summary.get("avg_hashtags_per_post"),
        "posts_with_cta_caption_pct": summary.get("posts_with_cta_caption_pct"),
        "avg_follower_following_ratio": summary.get("avg_follower_following_ratio"),
    }
    # None (not an all-None dict) when nothing is derivable — e.g. a Facebook-page-only audit or a
    # pre-v2 stored result — so every consumer's truthiness guard hides the section uniformly
    # instead of the PDFs rendering a dangling "Content insights" heading with no body.
    _ci_values = [v for k, v in content_insights.items() if k != "content_mix"]
    _ci_values.extend(content_insights["content_mix"].values())
    if all(v is None for v in _ci_values):
        content_insights = None

    # Top performing posts across all audited profiles (directly answers "are my views low?").
    top_posts: list[JsonDict] = []
    for profile in platforms:
        for post in profile.get("top_posts") or []:
            if isinstance(post, dict):
                top_posts.append({**post, "platform": profile.get("platform")})
    # Views and engagement are incommensurable across platforms, so their sum is a deterministic
    # cross-platform attention proxy (a viral image is not outranked by a 5-view video).
    top_posts.sort(key=lambda p: (p.get("views") or 0) + (p.get("engagement") or 0), reverse=True)
    top_posts = top_posts[:5]

    # Per-platform scorecard so a multi-platform audit can say "Facebook is stale, Instagram active"
    # instead of collapsing everything into one cross-profile average.
    per_platform = [
        {
            "platform": p.get("platform"),
            "handle": p.get("handle"),
            "followers": p.get("followers"),
            "posts_per_month": p.get("posts_per_month"),
            "days_since_last_post": p.get("days_since_last_post"),
            "avg_engagement_rate_pct": p.get("avg_engagement_rate_pct"),
            "video_share_pct": p.get("video_share_pct"),
            "avg_views_per_post": p.get("avg_views_per_post"),
            "total_views": p.get("total_views"),
            "verified": p.get("verified"),
            "is_business": p.get("is_business"),
            "profile_complete": p.get("profile_complete"),
            "link_in_bio": bool(p.get("link_in_bio")),
            "has_cta": p.get("has_cta"),
        }
        for p in platforms
    ]

    return {
        "version": SOCIAL_REPORT_VERSION,
        "score": social_score,
        "status": facts.get("status") or "unknown",
        "handles": {
            platform: _display_handle(handle) for platform, handle in _dict(handles).items()
        },
        "generated_date": datetime.now(UTC).strftime("%B %d, %Y"),
        "platforms_audited": summary.get("platforms_audited", 0),
        "summary": summary,
        "platforms": platforms,
        "executive_summary": content.get("executive_summary") or "",
        "commentary_provider": commentary.get("provider") or "deterministic",
        "findings": findings,
        "strengths": strengths,
        "content_insights": content_insights,
        "top_posts": top_posts,
        "per_platform": per_platform,
        "google_business": google_business,
        "connected_youtube": connected_youtube,
        "roadmap": roadmap,
    }


def compose_social_report_payload(job: Any, result: Any) -> JsonDict:
    return build_social_report_data(
        social_facts=getattr(result, "social_facts", None),
        social_breakdown=getattr(result, "score_breakdown", None),
        social_score=getattr(result, "social_score", None),
        handles=getattr(job, "social_handles", None),
        commentary=getattr(result, "commentary", None),
    )
