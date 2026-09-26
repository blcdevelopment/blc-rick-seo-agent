from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.worker.stages.ai_visibility.report import build_ai_visibility_report_data
from apps.worker.stages.benchmarking.report import build_benchmark_report_data
from apps.worker.stages.social.report import build_social_report_data

JsonDict = dict[str, Any]
ReportSectionId = Literal["seo", "uxui", "lead_generation"]
RecommendationTier = Literal["quick_win", "mid_term", "long_term"]

REPORT_PAYLOAD_VERSION = "phase1-report-v3"
TIER_LABELS: dict[RecommendationTier, str] = {
    "quick_win": "Quick Wins (0-30 days)",
    "mid_term": "Mid-Term Improvements (1-3 months)",
    "long_term": "Long-Term Growth Strategy (3-12 months)",
}
SECTION_LABELS: dict[ReportSectionId, str] = {
    "seo": "SEO",
    "uxui": "UX/UI",
    "lead_generation": "Lead Generation",
}


def _sentence(*parts: str) -> str:
    return " ".join(parts)


TECHNICAL_ISSUE_GUIDANCE: dict[str, dict[str, str]] = {
    "client_error_internal_urls": {
        "summary": _sentence(
            "These URLs on the site returned a 4xx error (for example '404 not found'),",
            "which usually means the page is blocked, removed, or unavailable.",
        ),
        "why_it_matters": _sentence(
            "Visitors and search engines can hit a dead end.",
            "If important pages link to these URLs, trust, search visibility,",
            "and user experience can suffer.",
        ),
        "recommended_fix": _sentence(
            "Update the link to the correct live URL, redirect the old URL",
            "to a useful replacement, or remove the link if the destination",
            "should no longer be used.",
        ),
        "location_label": "Affected URL found during the check",
    },
    "server_error_internal_urls": {
        "summary": "These URLs on the site returned a server error instead of a usable page.",
        "why_it_matters": _sentence(
            "Server errors can prevent users and Google from accessing",
            "important content.",
        ),
        "recommended_fix": _sentence(
            "Check the server/application logs for the affected URL,",
            "fix the underlying error, and confirm the page returns",
            "a normal 200 response.",
        ),
        "location_label": "Affected URL",
    },
    "client_error_external_urls": {
        "summary": _sentence(
            "These outside websites that this site links to returned a 4xx error,",
            "which usually means the destination blocked the request, moved,",
            "or is no longer available.",
        ),
        "why_it_matters": _sentence(
            "External broken links can frustrate visitors and make supporting",
            "resources, podcasts, or social proof harder to access.",
        ),
        "recommended_fix": _sentence(
            "Open each affected external link in a browser. Replace it with",
            "the current working URL, remove it, or use a more reliable",
            "destination if the third-party site blocks automated visits.",
        ),
        "location_label": "External URL returning a 4xx response",
    },
    "server_error_external_urls": {
        "summary": "These outside websites that this site links to returned a server error.",
        "why_it_matters": _sentence(
            "Visitors may not be able to access the third-party resource",
            "the page is pointing them toward.",
        ),
        "recommended_fix": _sentence(
            "Verify the destination manually and replace or remove links",
            "that consistently fail.",
        ),
        "location_label": "External URL returning a 5xx response",
    },
    "non_indexable_internal_urls": {
        "summary": _sentence(
            "These pages are marked or detected as non-indexable,",
            "so Google may not be able to include them in search results.",
        ),
        "why_it_matters": _sentence(
            "If these are important landing pages, they may not bring",
            "organic traffic even if the content is useful.",
        ),
        "recommended_fix": _sentence(
            "Confirm whether each page should rank. For pages that should rank,",
            "remove accidental noindex directives, blocked canonicals,",
            "or other indexability blockers.",
        ),
        "location_label": "Non-indexable page",
    },
    "redirect_chain_internal_urls": {
        "summary": _sentence(
            "These internal links go through two or more redirects before reaching",
            "the final page.",
        ),
        "why_it_matters": _sentence(
            "Each extra hop makes search engines work harder to reach the page,",
            "slows it for visitors,",
            "and can dilute the link's ranking signal.",
        ),
        "recommended_fix": _sentence(
            "Update the internal link to point straight at the final destination URL",
            "so there are no intermediate redirect hops.",
        ),
        "location_label": "Internal link with a redirect chain",
    },
    "missing_titles": {
        "summary": _sentence(
            "These pages are missing a title tag, which is the main title",
            "Google can show in search results.",
        ),
        "why_it_matters": _sentence(
            "A missing title makes the page harder for Google and users",
            "to understand, and can reduce click-through from search.",
        ),
        "recommended_fix": _sentence(
            "Add a unique, specific title tag that describes the page",
            "and includes the primary search topic.",
        ),
        "location_label": "Page missing a title tag",
    },
    "duplicate_titles": {
        "summary": "Multiple pages use the same title tag.",
        "why_it_matters": _sentence(
            "Duplicate titles make pages look interchangeable to Google",
            "and can cause the wrong page to rank or receive clicks.",
        ),
        "recommended_fix": _sentence(
            "Rewrite titles so each page has a unique title that reflects",
            "its specific topic, offer, or audience.",
        ),
        "location_label": "Page using a duplicate title",
    },
    "missing_meta_descriptions": {
        "summary": _sentence(
            "These pages are missing meta descriptions, the short summaries",
            "that can appear below a search result.",
        ),
        "why_it_matters": _sentence(
            "A missing description can lower click-through because Google",
            "may generate a less persuasive snippet automatically.",
        ),
        "recommended_fix": _sentence(
            "Write a concise description for each important page that explains",
            "the value of the page and gives users a reason to click.",
        ),
        "location_label": "Page missing a meta description",
    },
    "duplicate_meta_descriptions": {
        "summary": "Multiple pages use the same meta description.",
        "why_it_matters": _sentence(
            "Repeated descriptions make distinct pages harder to differentiate",
            "in search results.",
        ),
        "recommended_fix": _sentence(
            "Give each important page a unique description that matches",
            "that page's specific content and intent.",
        ),
        "location_label": "Page using a duplicate meta description",
    },
    "missing_h1": {
        "summary": _sentence(
            "These pages are missing a clear H1 heading, usually the main",
            "visible heading on the page.",
        ),
        "why_it_matters": _sentence(
            "Without a clear main heading, users may have a harder time",
            "understanding the page quickly, and search engines get a weaker",
            "topic signal.",
        ),
        "recommended_fix": _sentence(
            "Add one clear H1 that describes the page's main topic or offer.",
            "Avoid using multiple unrelated H1s on the same page.",
        ),
        "location_label": "Page missing an H1 heading",
    },
    "images_missing_alt": {
        "summary": _sentence(
            "Images are missing alt text, which describes the image for",
            "screen readers and search engines.",
        ),
        "why_it_matters": _sentence(
            "Missing alt text weakens accessibility and can reduce",
            "image/context relevance signals.",
        ),
        "recommended_fix": _sentence(
            "Add short, useful alt text to meaningful images. Decorative images",
            "can use empty alt text if they do not add information.",
        ),
        "location_label": "Image missing alt text",
    },
    "missing_canonicals": {
        "summary": _sentence(
            "These pages do not declare a canonical URL, which tells Google",
            "which version of a page should be treated as the preferred version.",
        ),
        "why_it_matters": _sentence(
            "Without canonicals, duplicate or similar URLs can split ranking",
            "signals or cause Google to choose a less ideal version.",
        ),
        "recommended_fix": _sentence(
            "Add a self-referencing canonical tag on indexable pages, or point",
            "duplicate pages to the preferred canonical URL.",
        ),
        "location_label": "Page missing a canonical URL",
    },
    "unreachable_internal_urls": {
        "summary": _sentence(
            "These URLs on the site did not respond when checked - the request",
            "timed out or the connection failed, so no page came back at all.",
        ),
        "why_it_matters": _sentence(
            "A link that never loads is a dead end for visitors and search",
            "engines, and can hide a hosting or configuration problem.",
        ),
        "recommended_fix": _sentence(
            "Open each URL in a browser. If it loads for you, the server may be",
            "blocking automated checks (usually safe to ignore). If it does not",
            "load, fix or remove the link and check the hosting setup.",
        ),
        "location_label": "URL that did not respond",
    },
}
GENERIC_TECHNICAL_ISSUE_GUIDANCE = {
    "summary": "The site health check found an issue that should be reviewed.",
    "why_it_matters": _sentence(
        "Technical issues can make pages harder for users or search engines",
        "to access, understand, or prioritize.",
    ),
    "recommended_fix": _sentence(
        "Review the affected URLs, confirm the issue is valid, and fix the",
        "page template or content source that creates it.",
    ),
    "location_label": "Affected URL",
}

# Friendly labels for internal pipeline statuses; the PDF/DOCX must never show
# raw machine tokens like "skipped" or "oauth_not_configured" to a client.
STATUS_LABELS: dict[str, str] = {
    "complete": "Collected",
    "partial": "Partially collected",
    "failed": "Not available (collection failed)",
    "skipped": "Not collected",
    "empty": "No data returned",
    "disabled": "Not enabled",
    "unknown": "Unknown",
}
SKIP_REASON_LABELS: dict[str, str] = {
    "disabled": "This data source is not enabled for this audit.",
    "not_collected": "This data source was not collected for this audit.",
    "collector_error": "The data collection step hit an unexpected error.",
    "oauth_not_configured": "Google Search Console is not connected for this workspace.",
    "no_google_connection": "No Google account has been connected yet.",
    "no_matching_search_console_property": (
        "The connected Google account does not have a verified Search Console "
        "property for this site."
    ),
    "screaming_frog_binary_not_found": ("Screaming Frog is not installed on the audit worker."),
    "screaming_frog_timeout": "The extended site health check ran out of time.",
    "missing_google_psi_api_key": (
        "No Google PageSpeed API key is configured, so page speed was not measured."
    ),
    "no_pages_to_analyze": "No analyzed pages were available to measure.",
    "bot_blocked": (
        "The site's server or firewall throttled our automated link checker before it "
        "could finish, so link checks are incomplete and were not scored. Spot-check "
        "important pages manually."
    ),
    "rate_limited": (
        "The site's server rate-limited our automated link checker on many pages, so link "
        "checks are incomplete and were not scored. The affected URLs are reported as "
        "unchecked, not broken — spot-check important pages manually."
    ),
    "no_link_coverage": (
        "Not one of the site's URLs answered our automated link checker (every request was "
        "throttled or refused), so link checks were not scored. This is a server/firewall "
        "behaviour, not evidence that the pages are broken — spot-check them manually."
    ),
    "time_budget": (
        "The link check reached its time budget before covering the whole site, so the "
        "results are incomplete and were not scored. The unchecked URLs are not reported as "
        "broken — re-run the audit or spot-check important pages manually."
    ),
}
TECHNICAL_CRAWL_TOOL_LABELS: dict[str, str] = {
    "screaming_frog_csv": "Screaming Frog SEO Spider",
    "screaming_frog_cli": "Screaming Frog SEO Spider",
    "site_health_sweep": "BLC site health check (built-in)",
}


def status_label(status: Any) -> str:
    return STATUS_LABELS.get(str(status or "unknown"), str(status or "unknown"))


def _reason_label(reason: Any) -> str | None:
    if not reason:
        return None
    return SKIP_REASON_LABELS.get(str(reason), str(reason))


# Fields in GSC table rows that are counts: the Search Console API returns them as floats, and
# rendering them raw prints "1065.0 clicks" in the client-facing tables. Coerced to int at the
# ONE payload seam so the PDF, DOCX, and web UI all show integers; CTR/position stay floats.
_GSC_COUNT_KEYS = (
    "clicks",
    "impressions",
    "current_clicks",
    "previous_clicks",
    "click_delta",
    "current_impressions",
    "previous_impressions",
)


def _int_counts(row: JsonDict) -> JsonDict:
    out = dict(row)
    for key in _GSC_COUNT_KEYS:
        value = out.get(key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and float(value).is_integer()
        ):
            out[key] = int(value)
    return out


class ReportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    source_url: str
    final_url: str
    site_domain: str
    niche: str | None = None
    target_audience: str | None = None
    generated_at: datetime
    generated_date: str
    pages_crawled: int = 0
    failed_pages: int = 0
    skipped_pages: int = 0
    rubric_version: str
    llm_model: str


class ScoreCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["lead_gen", "seo", "uxui"]
    label: str
    score: int = Field(ge=0, le=100)
    max_score: int = 100
    description: str
    # Same banding the operator UI uses (lib/format.ts): >=75 strong / >=50 fair / <50 weak,
    # so a non-technical reader gets context for the number.
    band: Literal["strong", "fair", "weak"] = "fair"
    band_label: str = ""


class ReportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: ReportSectionId
    severity: Literal["info", "low", "medium", "high"]
    title: str
    # Structured card fields (preferred by the PDF). ``explanation`` is the combined
    # single-paragraph form, kept for the DOCX export and as a render fallback.
    meaning: str = ""
    why: str = ""
    explanation: str
    location_label: str = ""
    location_urls: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    # The fix carried on the finding card ("Do this"), so problem + remedy render as one
    # card. Defaults keep results stored before this field existed rendering unchanged.
    action_items: list[str] = Field(default_factory=list)
    tier: str = ""
    source: Literal["commentary", "rubric"] = "commentary"


class ReportRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: ReportSectionId
    tier: RecommendationTier
    title: str
    rationale: str
    action_items: list[str] = Field(default_factory=list)
    location_label: str = ""
    location_urls: list[str] = Field(default_factory=list)


class RoadmapTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: RecommendationTier
    label: str
    recommendations: list[ReportRecommendation] = Field(default_factory=list)


class RuleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    description: str
    result: Literal["pass", "partial", "fail", "skipped"]
    points_awarded: float = 0.0
    points_possible: float = 0.0
    evidence_value: str | None = None
    reason: str | None = None


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ReportSectionId
    label: str
    headline: str
    score: int | None = Field(default=None, ge=0, le=100)
    findings: list[ReportFinding] = Field(default_factory=list)
    recommendations: list[ReportRecommendation] = Field(default_factory=list)
    # True only for LEGACY stored audits (pre finding-card commentary): their findings carry
    # no action_items, so the fixes live in `recommendations` and the surfaces must render
    # that list. Computed HERE — the one place — so the PDF/DOCX/UI can never disagree about
    # when the fallback applies. New payloads keep recommendations for the roadmap but leave
    # this False (the finding cards already carry every fix).
    show_recommendations: bool = False
    opportunities: list[RuleSummary] = Field(default_factory=list)


class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    numeric_claims_checked: int = 0
    unsupported_claim_count: int = 0
    action: str = "none"


class PageSpeedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    status_label: str = "Unknown"
    reason: str | None = None
    scope: str | None = None
    pages_requested: int = 0
    pages_analyzed: int = 0
    avg_mobile_performance: int | None = None
    avg_desktop_performance: int | None = None
    complete_mobile_pages: int = 0
    complete_desktop_pages: int = 0
    slowest_pages: list[JsonDict] = Field(default_factory=list)


class CwvMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value_label: str  # display-ready, e.g. "3.2 s", "90 ms", "0.004", "No data"
    rating: str  # good | needs_improvement | poor | unknown
    rating_label: str  # "Good" | "Needs improvement" | "Poor" | "No data"
    band: str  # strong | fair | weak | none  (drives the dot/cell colour)


class LabCwvRow(BaseModel):
    """One lab metric across both form factors, for the mobile/desktop table."""

    model_config = ConfigDict(extra="forbid")

    label: str
    mobile: CwvMetric | None = None
    desktop: CwvMetric | None = None


class CoreWebVitals(BaseModel):
    """Detailed PageSpeed Insights metrics, as shown on pagespeed.web.dev: the lab
    Core Web Vitals (mobile + desktop) plus the CrUX real-user field snapshot."""

    model_config = ConfigDict(extra="forbid")

    available: bool = False
    lab_rows: list[LabCwvRow] = Field(default_factory=list)
    field_available: bool = False
    field_source: str | None = None  # "Whole site (origin)" | "This page"
    field_form_factor: str = "mobile"
    field_assessment: str | None = None  # overall CrUX assessment label
    field_metrics: list[CwvMetric] = Field(default_factory=list)
    field_note: str | None = None


class ExternalSeoSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    technical_crawl_status: str = "skipped"
    technical_crawl_tool: str | None = None
    gsc_status: str = "skipped"
    url_inspection_status: str = "skipped"
    technical_issue_count: int = 0
    search_opportunity_count: int = 0


class TechnicalSeoIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Literal["info", "low", "medium", "high"]
    title: str
    count: int = 0
    summary: str
    why_it_matters: str
    recommended_fix: str
    location_label: str
    examples: list[str] = Field(default_factory=list)


class TechnicalSeoSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    status_label: str = "Not collected"
    reason_label: str | None = None
    source: str | None = None
    tool_label: str | None = None
    summary: JsonDict = Field(default_factory=dict)
    issues: list[TechnicalSeoIssue] = Field(default_factory=list)
    # Honest-coverage notes from the collector (e.g. "checked first 150 of 312 URLs").
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SearchPerformanceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    status_label: str = "Not collected"
    reason_label: str | None = None
    site_url: str | None = None
    date_range: JsonDict = Field(default_factory=dict)
    previous_date_range: JsonDict = Field(default_factory=dict)
    summary: JsonDict = Field(default_factory=dict)
    top_queries: list[JsonDict] = Field(default_factory=list)
    top_pages: list[JsonDict] = Field(default_factory=list)
    high_impression_low_ctr_pages: list[JsonDict] = Field(default_factory=list)
    ranking_opportunities: list[JsonDict] = Field(default_factory=list)
    declining_pages: list[JsonDict] = Field(default_factory=list)
    url_inspection_summary: JsonDict = Field(default_factory=dict)
    url_inspection_items: list[JsonDict] = Field(default_factory=list)
    # Business-opportunity framing (P1-P4) — all empty when GSC is not connected.
    opportunity: JsonDict = Field(default_factory=dict)
    branded: JsonDict = Field(default_factory=dict)
    topic_clusters: list[JsonDict] = Field(default_factory=list)


class AccessibilityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    impact: str
    wcag_criteria: list[str] = Field(default_factory=list)
    help: str = ""
    help_url: str = ""
    instances: int = 0
    example_selectors: list[str] = Field(default_factory=list)
    example_pages: list[str] = Field(default_factory=list)
    failure_summary: str = ""


class AccessibilityAdvisorySection(BaseModel):
    """Optional advisory accessibility section (axe-core). Advisory-only — populated only when
    the opt-in pass ran; NEVER derived from or affecting the scored sections."""

    model_config = ConfigDict(extra="forbid")

    status: str = "skipped"
    status_label: str = "Not collected"
    disclaimer: str = ""
    axe_version: str = ""
    pages_scanned: int = 0
    impact_counts: dict[str, int] = Field(default_factory=dict)
    needs_review_count: int = 0
    issues: list[AccessibilityIssue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CrawlSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    successful_pages: int = 0
    failed_pages: int = 0
    skipped_pages: int = 0
    failed_page_items: list[JsonDict] = Field(default_factory=list)
    skipped_page_items: list[JsonDict] = Field(default_factory=list)


class Appendix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scoring_note: str
    seo_rules: list[RuleSummary] = Field(default_factory=list)
    uxui_rules: list[RuleSummary] = Field(default_factory=list)


class ReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = REPORT_PAYLOAD_VERSION
    metadata: ReportMetadata
    scores: list[ScoreCard]
    executive_summary: str
    sections: list[ReportSection]
    roadmap: list[RoadmapTier]
    validation_summary: ValidationSummary
    pagespeed_summary: PageSpeedSummary
    core_web_vitals: CoreWebVitals = Field(default_factory=CoreWebVitals)
    external_seo_summary: ExternalSeoSummary
    technical_seo_section: TechnicalSeoSection
    search_performance_section: SearchPerformanceSection
    # Optional advisory accessibility section (default empty/"skipped" => not rendered). Like
    # core_web_vitals, this is default-factory so old stored results stay valid and the
    # REPORT_PAYLOAD_VERSION is unchanged.
    accessibility_advisory_section: AccessibilityAdvisorySection = Field(
        default_factory=AccessibilityAdvisorySection
    )
    crawl_summary: CrawlSummary
    # "What the whole website consists of" scope panel (pages, posts, sitemap, outbound, images).
    # Default None => not rendered, so a stored result without it stays valid and byte-identical.
    website_scope: JsonDict | None = None
    appendix: Appendix
    # Combined-audit only (default None => not rendered; a website-only report is byte-identical).
    # `social_audit` is the appended social-media section (same deterministic builder as the
    # standalone social report); `overall_readiness` is the blended Overall Lead-Gen Readiness
    # score. Both append at the END of the report and never alter the website sections above.
    social_audit: JsonDict | None = None
    overall_readiness: JsonDict | None = None
    # The ONE combined-cover predicate (overall status complete AND a real score), computed
    # once in compose_report_payload and read by the PDF cover, the DOCX title, and the
    # lead-gen score card — the three can never disagree about whether this report is a
    # combined one. False for website-only and "website_only"-overall payloads.
    combined_complete: bool = False
    # Enrichment: Competitor Benchmarking (P2-26 / SMWA-79 — deferred v3, default None => not
    # rendered; a report without benchmark data is byte-identical). Presentation only — appended at
    # the END and never alters the sections above.
    benchmark: JsonDict | None = None
    # Enrichment: AI Visibility (Semrush AI Visibility Toolkit — on-demand, default None => not
    # rendered; a report without AI-visibility data is byte-identical). Presentation only.
    ai_visibility: JsonDict | None = None


def compose_report_payload(job: Any, result: Any) -> ReportPayload:
    crawled_pages = _dict(result.crawled_pages)
    score_breakdown = _dict(result.score_breakdown)
    commentary = _dict(result.commentary)
    external_seo_facts = _dict(getattr(result, "external_seo_facts", None))

    final_url = str(crawled_pages.get("final_url") or job.url)
    generated_at = _report_generated_at(result)
    metadata = ReportMetadata(
        audit_id=job.id,
        source_url=str(job.url),
        final_url=final_url,
        site_domain=_domain(final_url),
        niche=job.niche,
        target_audience=job.target_audience,
        generated_at=generated_at,
        generated_date=f"{generated_at.strftime('%B')} {generated_at.day}, {generated_at.year}",
        pages_crawled=int(crawled_pages.get("summary", {}).get("successful_pages") or 0),
        failed_pages=int(crawled_pages.get("summary", {}).get("failed_pages") or 0),
        skipped_pages=int(crawled_pages.get("summary", {}).get("skipped_pages") or 0),
        rubric_version=str(result.rubric_version),
        llm_model=str(result.llm_model),
    )

    sections = [
        _compose_section("seo", result.seo_score, commentary, score_breakdown),
        _compose_section("uxui", result.uxui_score, commentary, score_breakdown),
        _compose_section("lead_generation", result.lead_gen_score, commentary, score_breakdown),
    ]

    # Combined-audit extras (appended at the END of the report), each keyed off its own data so
    # neither drags in a degenerate version of the other: the social section renders only when
    # collected social facts exist on the result, and the overall score renders only when the
    # breakdown carries it. For a website-only audit both stay None and the report is
    # byte-identical to before.
    social_facts = _dict(getattr(result, "social_facts", None))
    overall_readiness = score_breakdown.get("overall_readiness")
    overall_readiness = overall_readiness if isinstance(overall_readiness, dict) else None
    # The ONE combined-cover predicate every surface shares (PDF cover, DOCX title, the
    # lead-gen card intro): a "website_only" overall (failed social collection) carries a
    # real score but no social section, so it must NOT read as combined anywhere.
    combined_complete = bool(
        overall_readiness
        and overall_readiness.get("status") == "complete"
        and overall_readiness.get("score") is not None
    )
    social_audit = None
    if social_facts:
        social_audit = build_social_report_data(
            social_facts=social_facts,
            social_breakdown=score_breakdown.get("social"),
            social_score=getattr(result, "social_score", None),
            handles=getattr(job, "social_handles", None),
            commentary=None,
        )

    # Enrichment: Competitor Benchmarking (deferred v3). Populated only when a benchmark ran and
    # left facts in score_breakdown["benchmark"]; otherwise None => the section is not rendered.
    # `overall` is present only for combined audits (a website-only audit has no Overall Readiness),
    # so an overall-only baseline is intentionally not compared on a website audit — you cannot
    # benchmark a score that does not exist; the other four metrics still compare.
    benchmark = build_benchmark_report_data(
        scores={
            "seo": result.seo_score,
            "uxui": result.uxui_score,
            "lead_gen": result.lead_gen_score,
            "social": getattr(result, "social_score", None),
            "overall": (overall_readiness or {}).get("score"),
        },
        benchmark_facts=score_breakdown.get("benchmark"),
    )

    # Enrichment: AI Visibility (Semrush). Populated only when the on-demand AI-visibility rerun
    # stored facts in score_breakdown["ai_visibility"]; otherwise None => no section is rendered
    # and the report is byte-identical.
    ai_visibility = build_ai_visibility_report_data(score_breakdown.get("ai_visibility"))

    return ReportPayload(
        metadata=metadata,
        scores=_score_cards(result, score_breakdown, combined_complete=combined_complete),
        executive_summary=_executive_summary(commentary),
        sections=sections,
        roadmap=_roadmap(sections),
        validation_summary=_validation_summary(_dict(result.validation_log)),
        pagespeed_summary=_pagespeed_summary(_dict(result.psi_facts)),
        core_web_vitals=_core_web_vitals(_dict(result.psi_facts)),
        external_seo_summary=_external_seo_summary(external_seo_facts),
        technical_seo_section=_technical_seo_section(external_seo_facts),
        search_performance_section=_search_performance_section(external_seo_facts),
        accessibility_advisory_section=_accessibility_advisory_section(
            _dict(getattr(result, "accessibility_facts", None))
        ),
        crawl_summary=_crawl_summary(crawled_pages),
        website_scope=_website_scope(external_seo_facts, crawled_pages, _dict(result.seo_facts)),
        appendix=_appendix(score_breakdown),
        social_audit=social_audit,
        overall_readiness=overall_readiness,
        combined_complete=combined_complete,
        benchmark=benchmark,
        ai_visibility=ai_visibility,
    )


def _compose_section(
    section_id: ReportSectionId,
    score: int,
    commentary: JsonDict,
    score_breakdown: JsonDict,
) -> ReportSection:
    section_content = _dict(_dict(commentary.get("content")).get(section_id))
    label = SECTION_LABELS[section_id]
    headline = str(section_content.get("headline") or f"{label} score is {score}")
    opportunities = _opportunities_for_section(section_id, score_breakdown)
    findings = _commentary_findings(section_id, section_content)
    if not findings:
        findings = _fallback_findings(section_id, score, opportunities)
    # The section guide tells readers to start with high/medium findings, so render
    # them in that order (stable sort keeps the within-severity authoring order).
    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings = sorted(findings, key=lambda finding: severity_rank[finding.severity])
    recommendations = _commentary_recommendations(section_id, section_content)

    return ReportSection(
        id=section_id,
        label=label,
        headline=headline,
        score=score,
        findings=findings,
        recommendations=recommendations,
        show_recommendations=bool(recommendations)
        and all(not finding.action_items for finding in findings),
        opportunities=opportunities,
    )


def _score_cards(
    result: Any, score_breakdown: JsonDict, *, combined_complete: bool
) -> list[ScoreCard]:
    scores = _dict(score_breakdown.get("scores"))
    composite = _dict(score_breakdown.get("composite"))
    weights = _dict(composite.get("weights"))
    seo_weight_value = _weight_value(weights.get("seo"), 0.45)
    uxui_weight_value = _weight_value(weights.get("uxui"), 0.55)
    seo_weight = _percent_weight(seo_weight_value, "45%")
    uxui_weight = _percent_weight(uxui_weight_value, "55%")
    seo_score = int(scores.get("seo") or result.seo_score)
    uxui_score = int(scores.get("uxui") or result.uxui_score)
    lead_gen_score = int(result.lead_gen_score)
    # Half-up rounding, matching the scoring engine's project-wide convention
    # (scoring._round_score) so the printed formula never contradicts the stored score.
    calculated_lead_gen = int(
        (seo_score * seo_weight_value) + (uxui_score * uxui_weight_value) + 0.5
    )
    lead_gen_formula_result = (
        f"{calculated_lead_gen}/100, which is the audit score."
        if calculated_lead_gen == lead_gen_score
        else f"{calculated_lead_gen}/100. The stored audit score is {lead_gen_score}/100."
    )
    # On a combined audit the headline score is the Overall Lead-Gen Readiness, so the
    # website composite is renamed to say exactly what it covers — two similarly-named
    # "combined" scores with different formulas read as a contradiction. `combined_complete`
    # is computed ONCE in compose_report_payload and shared with the PDF cover and DOCX
    # title, so this intro can never point the reader at a social/overall section those
    # surfaces decided not to render.
    is_combined = combined_complete
    lead_gen_label = "Website Lead-Gen Score" if is_combined else "Lead Generation Readiness"
    lead_gen_intro = (
        "This is the combined business-readiness score for the website (the social audit "
        "and the Overall Lead-Gen Readiness are reported at the end of this report). "
        if is_combined
        else "This is the combined business-readiness score for the website. "
    )
    return [
        ScoreCard(
            id="lead_gen",
            label=lead_gen_label,
            score=lead_gen_score,
            band=_score_band(lead_gen_score),
            band_label=_score_band_label(lead_gen_score),
            description=(
                f"{lead_gen_intro}"
                f"Formula: round((SEO {seo_score} * {seo_weight}) + "
                f"(UX/UI {uxui_score} * {uxui_weight})) = "
                f"{lead_gen_formula_result}"
            ),
        ),
        ScoreCard(
            id="seo",
            label="SEO",
            score=int(result.seo_score),
            band=_score_band(int(result.seo_score)),
            band_label=_score_band_label(int(result.seo_score)),
            description=(
                "This score comes from checks for search visibility, metadata, site health, "
                "indexability, Search Console opportunity, PageSpeed, links, and schema. "
                + _score_calculation_sentence("seo", score_breakdown, int(result.seo_score))
            ).strip(),
        ),
        ScoreCard(
            id="uxui",
            label="UX/UI",
            score=int(result.uxui_score),
            band=_score_band(int(result.uxui_score)),
            band_label=_score_band_label(int(result.uxui_score)),
            description=(
                "This score comes from checks for conversion clarity, calls to action, lead "
                "forms, contact paths, trust proof, navigation, and homepage clarity. "
                + _score_calculation_sentence("uxui", score_breakdown, int(result.uxui_score))
            ).strip(),
        ),
    ]


def _score_band(score: int) -> Literal["strong", "fair", "weak"]:
    if score >= 75:
        return "strong"
    if score >= 50:
        return "fair"
    return "weak"


def _score_band_label(score: int) -> str:
    return {"strong": "Strong", "fair": "Fair", "weak": "Needs work"}[_score_band(score)]


def _percent_weight(value: Any, fallback: str) -> str:
    # Half-up (project convention int(x + 0.5)), NOT round(): the printed weights appear in the
    # score card's "Formula:" line beside a half-up composite, so banker's rounding would make a
    # retuned rubric (e.g. seo 0.425) print weights that don't reproduce the stated score.
    if isinstance(value, int | float):
        return f"{int(float(value) * 100 + 0.5)}%"
    return fallback


def _weight_value(value: Any, fallback: float) -> float:
    if isinstance(value, int | float):
        return float(value)
    return fallback


def _score_calculation_sentence(
    section_id: ReportSectionId,
    score_breakdown: JsonDict,
    score: int,
) -> str:
    if section_id == "lead_generation":
        return ""

    category = _dict(_dict(score_breakdown.get("categories")).get(section_id))
    rules = [_dict(rule) for rule in _list(category.get("rules"))]
    evaluated_rules = [rule for rule in rules if rule.get("result") != "skipped"]
    skipped_rules = [rule for rule in rules if rule.get("result") == "skipped"]
    awarded_points = sum(_rule_points_awarded(rule) for rule in evaluated_rules)
    possible_points = _evaluated_points_possible(category, evaluated_rules)

    if not evaluated_rules or possible_points <= 0:
        calculation = (
            f"No {SECTION_LABELS[section_id]} checks had enough source data to score, "
            f"so this section is shown as {score}/100 until the needed facts are available."
        )
    else:
        evaluated_label = _plural(len(evaluated_rules), "check", "checks")
        # When the earned points already read as the /100 score, the normalization
        # clause would restate the same number — drop it as redundant.
        if possible_points == 100 and _format_points(awarded_points) == str(score):
            normalization = ""
        else:
            normalization = f"; those evaluated points are normalized to {score}/100"
        calculation = (
            f"It evaluated {len(evaluated_rules)} {evaluated_label} and earned "
            f"{_format_points(awarded_points)} of {_format_points(possible_points)} "
            f"available points{normalization}."
        )

    skipped = ""
    if skipped_rules:
        skipped_points = _skipped_points(category, skipped_rules)
        skipped_label = _plural(len(skipped_rules), "check", "checks")
        point_label = _plural(skipped_points, "point", "points")
        skipped_verb = _plural(len(skipped_rules), "was", "were")
        skipped = (
            f" {len(skipped_rules)} {skipped_label} worth "
            f"{_format_points(skipped_points)} {point_label} {skipped_verb} skipped because "
            "the needed source data was unavailable; skipped checks are not counted as failures."
        )

    return f"{calculation}{skipped} {_score_driver_sentence(section_id, score_breakdown)}"


def _evaluated_points_possible(category: JsonDict, evaluated_rules: list[JsonDict]) -> float:
    weights = _dict(category.get("weights"))
    evaluated_weight = weights.get("evaluated")
    if isinstance(evaluated_weight, int | float) and float(evaluated_weight) > 0:
        return float(evaluated_weight)
    return sum(_rule_points_possible(rule) for rule in evaluated_rules)


def _skipped_points(category: JsonDict, skipped_rules: list[JsonDict]) -> float:
    weights = _dict(category.get("weights"))
    skipped_weight = weights.get("skipped")
    if isinstance(skipped_weight, int | float) and float(skipped_weight) > 0:
        return float(skipped_weight)
    return sum(float(rule.get("weight") or 0) for rule in skipped_rules)


def _rule_points_awarded(rule: JsonDict) -> float:
    value = rule.get("points_awarded")
    return float(value) if isinstance(value, int | float) else 0.0


def _rule_points_possible(rule: JsonDict) -> float:
    value = rule.get("points_possible")
    if isinstance(value, int | float):
        return float(value)
    weight = rule.get("weight")
    return float(weight) if isinstance(weight, int | float) else 0.0


def _format_points(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _plural(count: float | int, singular: str, plural: str) -> str:
    return singular if float(count) == 1 else plural


def _score_driver_sentence(section_id: ReportSectionId, score_breakdown: JsonDict) -> str:
    if section_id == "lead_generation":
        return ""
    category = _dict(_dict(score_breakdown.get("categories")).get(section_id))
    rules = [
        _dict(rule)
        for rule in _list(category.get("rules"))
        if _dict(rule).get("result") in {"fail", "partial"}
        and _dict(rule).get("surface_as_finding", True)
    ]
    if not rules:
        return "No high-priority deductions were found in this section."

    ranked = sorted(
        rules,
        key=lambda rule: (
            -float(rule.get("points_possible") or rule.get("weight") or 0),
            str(rule.get("rule_id") or ""),
        ),
    )
    labels = [_rule_label(rule) for rule in ranked[:3]]
    return "The biggest deductions came from " + _human_join(labels) + "."


def _rule_label(rule: JsonDict) -> str:
    label = rule.get("finding_label") or rule.get("description") or rule.get("rule_id")
    return " ".join(str(label or "an audit check").split()).rstrip(".")


def _human_join(values: list[str]) -> str:
    cleaned = [value for value in values if value]
    if not cleaned:
        return "the listed findings"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _executive_summary(commentary: JsonDict) -> str:
    content = _dict(commentary.get("content"))
    summary = str(content.get("executive_summary") or "").strip()
    if summary:
        return summary
    return (
        "The audit produced deterministic SEO, UX/UI, and Lead Generation Readiness scores. "
        "Use the prioritized roadmap and score breakdown to address the highest-confidence "
        "lead generation opportunities first."
    )


def _commentary_findings(
    section_id: ReportSectionId,
    section_content: JsonDict,
) -> list[ReportFinding]:
    findings: list[ReportFinding] = []
    for item in _list(section_content.get("findings")):
        payload = _dict(item)
        meaning = _text(payload.get("meaning"), "")
        why = _text(payload.get("why"), "")
        # Compose the legacy single-paragraph explanation (DOCX + render fallback) from the
        # structured fields when the content plan did not supply one.
        explanation = (
            _text(payload.get("explanation"), "")
            or " ".join(part for part in (meaning, why) if part).strip()
            or "No explanation provided."
        )
        findings.append(
            ReportFinding(
                section=section_id,
                severity=_severity(payload.get("severity")),
                title=_text(payload.get("title"), "Finding"),
                meaning=meaning,
                why=why,
                explanation=explanation,
                location_label=_text(payload.get("location_label"), ""),
                location_urls=[
                    str(value) for value in _list(payload.get("location_urls")) if value
                ],
                evidence_refs=[str(value) for value in _list(payload.get("evidence_refs"))],
                action_items=[str(value) for value in _list(payload.get("action_items")) if value],
                tier=_text(payload.get("tier"), ""),
                source="commentary",
            )
        )
    return findings


def _commentary_recommendations(
    section_id: ReportSectionId,
    section_content: JsonDict,
) -> list[ReportRecommendation]:
    recommendations: list[ReportRecommendation] = []
    for item in _list(section_content.get("recommendations")):
        payload = _dict(item)
        recommendations.append(
            ReportRecommendation(
                section=section_id,
                tier=_tier(payload.get("tier")),
                title=_text(payload.get("title"), "Recommendation"),
                rationale=_text(payload.get("rationale"), "Recommended from audit evidence."),
                action_items=[str(value) for value in _list(payload.get("action_items"))],
                location_label=_text(payload.get("location_label"), ""),
                location_urls=[
                    str(value) for value in _list(payload.get("location_urls")) if value
                ],
            )
        )
    return recommendations


def _fallback_findings(
    section_id: ReportSectionId,
    score: int,
    opportunities: list[RuleSummary],
) -> list[ReportFinding]:
    # A SKIPPED rule is NOT an opportunity: it was never measured, and its positively-phrased
    # rubric description ("Homepage form length is practical for lead capture") would read as
    # an asserted result — with debug-speak underneath — in a client-facing findings list (and
    # in the exec-summary Top Findings box). Only rules the audit actually evaluated surface.
    measured = [rule for rule in opportunities if rule.result in {"fail", "partial"}]
    if measured:
        primary = measured[0]
        return [
            ReportFinding(
                section=section_id,
                severity="medium" if score < 70 else "info",
                title=primary.description,
                explanation=(
                    "The deterministic scoring rubric flagged this item as an opportunity "
                    f"with a {primary.result} result."
                ),
                evidence_refs=[primary.rule_id],
                source="rubric",
            )
        ]

    label = SECTION_LABELS[section_id]
    if score >= 75:
        # Nothing failed or partially failed: say so plainly instead of surfacing noise.
        return [
            ReportFinding(
                section=section_id,
                severity="info",
                title=f"No high-priority {label} issues were found",
                explanation=(
                    f"Every {label} check the audit could measure passed or scored within "
                    "range. Checks skipped for lack of data never lower the score."
                ),
                evidence_refs=["score_breakdown"],
                source="rubric",
            )
        ]
    return [
        ReportFinding(
            section=section_id,
            severity="info",
            title=f"{label} score is {score}",
            explanation=(
                f"The deterministic {label} score was generated from extracted audit facts."
            ),
            evidence_refs=["score_breakdown"],
            source="rubric",
        )
    ]


def _opportunities_for_section(
    section_id: ReportSectionId,
    score_breakdown: JsonDict,
) -> list[RuleSummary]:
    if section_id == "lead_generation":
        return []
    category = _dict(_dict(score_breakdown.get("categories")).get(section_id))
    rules = [_rule_summary(rule) for rule in _list(category.get("rules"))]
    return [rule for rule in rules if rule.result in {"fail", "partial", "skipped"}]


def _appendix(score_breakdown: JsonDict) -> Appendix:
    categories = _dict(score_breakdown.get("categories"))
    return Appendix(
        scoring_note=(
            "Scores are deterministic and generated from extracted website facts using versioned "
            "YAML rubrics. Commentary explains those facts; it does not create new scores."
        ),
        seo_rules=[
            _rule_summary(rule) for rule in _list(_dict(categories.get("seo")).get("rules"))
        ],
        uxui_rules=[
            _rule_summary(rule) for rule in _list(_dict(categories.get("uxui")).get("rules"))
        ],
    )


def _rule_summary(rule: Any) -> RuleSummary:
    payload = _dict(rule)
    evidence = _dict(payload.get("evidence"))
    return RuleSummary(
        rule_id=_text(payload.get("rule_id"), "unknown_rule"),
        description=_text(payload.get("description"), "No description provided."),
        result=_rule_result(payload.get("result")),
        points_awarded=float(payload.get("points_awarded") or 0),
        points_possible=float(payload.get("points_possible") or 0),
        evidence_value=_evidence_value(evidence.get("value")),
        reason=str(evidence.get("reason")) if evidence.get("reason") else None,
    )


def _roadmap(sections: list[ReportSection]) -> list[RoadmapTier]:
    recommendations = [
        recommendation
        for section in sections
        for recommendation in section.recommendations
        if section.id in {"seo", "uxui", "lead_generation"}
    ]
    return [
        RoadmapTier(
            tier=tier,
            label=label,
            recommendations=[
                recommendation for recommendation in recommendations if recommendation.tier == tier
            ],
        )
        for tier, label in TIER_LABELS.items()
    ]


def _validation_summary(validation_log: JsonDict) -> ValidationSummary:
    return ValidationSummary(
        status=str(validation_log.get("status") or "unknown"),
        numeric_claims_checked=int(validation_log.get("numeric_claims_checked") or 0),
        unsupported_claim_count=int(validation_log.get("unsupported_claim_count") or 0),
        action=str(validation_log.get("action") or "none"),
    )


def _pagespeed_summary(psi_facts: JsonDict) -> PageSpeedSummary:
    summary = _dict(psi_facts.get("summary"))
    psi_status = str(psi_facts.get("status") or "unknown")
    return PageSpeedSummary(
        status=psi_status,
        status_label=status_label(psi_status),
        reason=_reason_label(psi_facts.get("reason")),
        scope=str(psi_facts.get("scope")) if psi_facts.get("scope") else None,
        pages_requested=int(psi_facts.get("pages_requested") or 0),
        pages_analyzed=int(psi_facts.get("pages_analyzed") or 0),
        avg_mobile_performance=_optional_int(summary.get("avg_mobile_performance")),
        avg_desktop_performance=_optional_int(summary.get("avg_desktop_performance")),
        complete_mobile_pages=int(summary.get("complete_mobile_pages") or 0),
        complete_desktop_pages=int(summary.get("complete_desktop_pages") or 0),
        slowest_pages=[_dict(page) for page in _list(summary.get("slowest_pages"))],
    )


# Core Web Vitals thresholds (Google's standard Good / Needs-improvement boundaries, in
# native units: milliseconds for time metrics, a unitless score for CLS).
_LAB_THRESHOLDS = {
    "first_contentful_paint_ms": (1800, 3000),
    "largest_contentful_paint_ms": (2500, 4000),
    "speed_index_ms": (3400, 5800),
    "total_blocking_time_ms": (200, 600),
    "cumulative_layout_shift": (0.1, 0.25),
}
_FIELD_THRESHOLDS = {
    "largest_contentful_paint_ms": (2500, 4000),
    "interaction_to_next_paint_ms": (200, 500),
    "cumulative_layout_shift": (0.1, 0.25),
    "first_contentful_paint_ms": (1800, 3000),
    "time_to_first_byte_ms": (800, 1800),
}
# (key, label, unit) in display order. unit: "seconds" (ms -> s), "ms", or "cls".
_LAB_METRIC_DISPLAY = [
    ("first_contentful_paint_ms", "First Contentful Paint", "seconds"),
    ("largest_contentful_paint_ms", "Largest Contentful Paint", "seconds"),
    ("total_blocking_time_ms", "Total Blocking Time", "ms"),
    ("cumulative_layout_shift", "Cumulative Layout Shift", "cls"),
    ("speed_index_ms", "Speed Index", "seconds"),
]
_FIELD_METRIC_DISPLAY = [
    ("largest_contentful_paint_ms", "Largest Contentful Paint", "seconds"),
    ("interaction_to_next_paint_ms", "Interaction to Next Paint", "ms"),
    ("cumulative_layout_shift", "Cumulative Layout Shift", "cls"),
    ("first_contentful_paint_ms", "First Contentful Paint", "seconds"),
    ("time_to_first_byte_ms", "Time to First Byte", "ms"),
]
_RATING_LABELS = {
    "good": "Good",
    "needs_improvement": "Needs improvement",
    "poor": "Poor",
    "unknown": "No data",
}
_RATING_BANDS = {
    "good": "strong",
    "needs_improvement": "fair",
    "poor": "weak",
    "unknown": "none",
}
# CrUX's own assessment categories take precedence over deriving from the percentile.
_CRUX_CATEGORY_RATING = {"FAST": "good", "AVERAGE": "needs_improvement", "SLOW": "poor"}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_metric_value(value: Any, unit: str) -> str:
    if not _is_number(value):
        return "No data"
    if unit == "seconds":
        return f"{value / 1000:.1f} s"
    if unit == "ms":
        return f"{int(value + 0.5)} ms"
    if unit == "cls":
        return f"{value:.3f}"
    return str(value)


def _rate_value(value: Any, thresholds: tuple[float, float]) -> str:
    if not _is_number(value):
        return "unknown"
    good_max, ni_max = thresholds
    if value <= good_max:
        return "good"
    if value <= ni_max:
        return "needs_improvement"
    return "poor"


def _cwv_metric(key: str, label: str, unit: str, value: Any, thresholds: JsonDict) -> CwvMetric:
    rating = _rate_value(value, thresholds[key])
    return CwvMetric(
        key=key,
        label=label,
        value_label=_format_metric_value(value, unit),
        rating=rating,
        rating_label=_RATING_LABELS[rating],
        band=_RATING_BANDS[rating],
    )


def _field_cwv_metric(key: str, label: str, unit: str, metric: Any) -> CwvMetric:
    p75: Any = None
    rating = "unknown"
    metric = _dict(metric)
    if metric:
        p75 = metric.get("p75")
        category = metric.get("category")
        if isinstance(category, str) and category.upper() in _CRUX_CATEGORY_RATING:
            rating = _CRUX_CATEGORY_RATING[category.upper()]
        else:
            rating = _rate_value(p75, _FIELD_THRESHOLDS[key])
    return CwvMetric(
        key=key,
        label=label,
        value_label=_format_metric_value(p75, unit),
        rating=rating,
        rating_label=_RATING_LABELS[rating],
        band=_RATING_BANDS[rating],
    )


def _lab_metrics(strategy_facts: JsonDict) -> list[CwvMetric]:
    if str(strategy_facts.get("status")) != "complete":
        return []
    lab = _dict(strategy_facts.get("lab_metrics"))
    if not any(_is_number(lab.get(key)) for key, _, _ in _LAB_METRIC_DISPLAY):
        return []
    return [
        _cwv_metric(key, label, unit, lab.get(key), _LAB_THRESHOLDS)
        for key, label, unit in _LAB_METRIC_DISPLAY
    ]


def _lab_rows(mobile: JsonDict, desktop: JsonDict) -> list[LabCwvRow]:
    mob = _lab_metrics(mobile)
    des = _lab_metrics(desktop)
    if not mob and not des:
        return []
    rows: list[LabCwvRow] = []
    for index, (_key, label, _unit) in enumerate(_LAB_METRIC_DISPLAY):
        rows.append(
            LabCwvRow(
                label=label,
                mobile=mob[index] if index < len(mob) else None,
                desktop=des[index] if index < len(des) else None,
            )
        )
    return rows


def _select_field_experience(mobile: JsonDict, desktop: JsonDict) -> tuple[JsonDict, str, str]:
    """Pick the best available CrUX field block. Prefer origin-level (what Google shows
    as "this origin") over page-level, and mobile over desktop. Returns (experience,
    source_label, form_factor)."""
    for strategy, form_factor in ((mobile, "mobile"), (desktop, "desktop")):
        field = _dict(strategy.get("field_data"))
        for source, label in (("origin", "Whole site (origin)"), ("page", "This page")):
            experience = field.get(source)
            if isinstance(experience, dict):
                return experience, label, form_factor
    return {}, "", "mobile"


def _core_web_vitals(psi_facts: JsonDict) -> CoreWebVitals:
    strategies = _dict(psi_facts.get("strategies"))
    mobile = _dict(strategies.get("mobile"))
    desktop = _dict(strategies.get("desktop"))

    lab_rows = _lab_rows(mobile, desktop)

    experience, source_label, form_factor = _select_field_experience(mobile, desktop)
    field_metrics = [
        _field_cwv_metric(key, label, unit, experience.get(key))
        for key, label, unit in _FIELD_METRIC_DISPLAY
    ]
    field_available = any(metric.rating != "unknown" for metric in field_metrics)

    field_assessment = None
    overall = experience.get("overall_category") if experience else None
    if isinstance(overall, str) and overall.upper() in _CRUX_CATEGORY_RATING:
        field_assessment = _RATING_LABELS[_CRUX_CATEGORY_RATING[overall.upper()]]

    return CoreWebVitals(
        available=bool(lab_rows),
        lab_rows=lab_rows,
        field_available=field_available,
        field_source=source_label or None,
        field_form_factor=form_factor,
        field_assessment=field_assessment,
        field_metrics=field_metrics if field_available else [],
        field_note=(
            "Field data reflects real Chrome users over the most recent 28-day window."
            if field_available
            else None
        ),
    )


def _technical_crawl_facts(external_seo_facts: JsonDict) -> JsonDict:
    """The technical crawl slot; falls back to the legacy ``screaming_frog`` key so
    audits stored before the tool-neutral rename still render."""
    technical = external_seo_facts.get("technical_crawl")
    if isinstance(technical, dict) and technical:
        return technical
    return _dict(external_seo_facts.get("screaming_frog"))


def _website_scope(
    external_seo_facts: JsonDict, crawled_pages: JsonDict, seo_facts: JsonDict
) -> JsonDict | None:
    """ "What the whole website consists of" — a plain-language scope panel (Dru's request).

    Pure surfacing of already-collected numbers: total pages/posts/outbound come from the
    site-health sweep summary (site size, captured before its coverage cap), pages analyzed from
    the crawl, images from the extracted SEO facts. Returns ``None`` when nothing is known (e.g. a
    failed crawl) so the section simply doesn't render. Counts are honest estimates — the sweep
    discovers via internal links + sitemap, and 'posts' is a URL-pattern heuristic — so the labels
    say "discovered"/"detected" rather than implying an exhaustive CMS count.
    """
    tsummary = _dict(_technical_crawl_facts(external_seo_facts).get("summary"))
    pages_analyzed = int(_dict(crawled_pages.get("summary")).get("successful_pages") or 0)
    images = sum(
        int(_dict(page.get("images")).get("total") or 0) for page in _list(seo_facts.get("pages"))
    )

    def _pos(value: Any) -> int | None:
        number = int(value) if isinstance(value, (int, float)) else 0
        return number if number > 0 else None

    scope = {
        "pages_discovered": _pos(tsummary.get("discovered_internal_urls")),
        "pages_analyzed": pages_analyzed or None,
        "blog_posts": _pos(tsummary.get("discovered_blog_posts")),
        "sitemap_entries": _pos(tsummary.get("sitemap_url_count")),
        "outbound_links": _pos(tsummary.get("discovered_external_urls")),
        "images": _pos(images),
    }
    return scope if any(v is not None for v in scope.values()) else None


def _external_seo_summary(external_seo_facts: JsonDict) -> ExternalSeoSummary:
    technical = _technical_crawl_facts(external_seo_facts)
    gsc = _dict(external_seo_facts.get("gsc"))
    url_inspection = _dict(external_seo_facts.get("url_inspection"))
    technical_complete = technical.get("status") == "complete"
    gsc_complete = gsc.get("status") == "complete"
    return ExternalSeoSummary(
        status=str(external_seo_facts.get("status") or "skipped"),
        technical_crawl_status=str(technical.get("status") or "skipped"),
        technical_crawl_tool=(
            TECHNICAL_CRAWL_TOOL_LABELS.get(str(technical.get("source")))
            if technical.get("source")
            else None
        ),
        gsc_status=str(gsc.get("status") or "skipped"),
        url_inspection_status=str(url_inspection.get("status") or "skipped"),
        technical_issue_count=(len(_list(technical.get("issues"))) if technical_complete else 0),
        search_opportunity_count=(
            len(_list(gsc.get("high_impression_low_ctr_pages")))
            + len(_list(gsc.get("ranking_opportunities")))
            + len(_list(gsc.get("declining_pages")))
            if gsc_complete
            else 0
        ),
    )


def _technical_seo_section(external_seo_facts: JsonDict) -> TechnicalSeoSection:
    technical = _technical_crawl_facts(external_seo_facts)
    status = str(technical.get("status") or "skipped")
    source_complete = status == "complete"
    raw_issues = _list(technical.get("issues")) if source_complete else []
    source = str(technical.get("source")) if technical.get("source") else None
    return TechnicalSeoSection(
        status=status,
        status_label=status_label(status),
        reason_label=_reason_label(technical.get("reason") or technical.get("error")),
        source=source,
        tool_label=TECHNICAL_CRAWL_TOOL_LABELS.get(source or "", source),
        summary=_dict(technical.get("summary")) if source_complete else {},
        issues=[_technical_issue(_dict(item)) for item in raw_issues],
        notes=[str(note) for note in _list(technical.get("notes")) if note],
        warnings=[str(warning) for warning in _list(technical.get("warnings")) if warning],
    )


def _technical_issue(issue: JsonDict) -> TechnicalSeoIssue:
    issue_id = _text(issue.get("id"), "technical_seo_issue")
    guidance = TECHNICAL_ISSUE_GUIDANCE.get(issue_id, GENERIC_TECHNICAL_ISSUE_GUIDANCE)
    return TechnicalSeoIssue(
        id=issue_id,
        severity=_severity(issue.get("severity")),
        title=_text(issue.get("title"), "Technical SEO issue"),
        count=int(issue.get("count") or 0),
        summary=guidance["summary"],
        why_it_matters=guidance["why_it_matters"],
        recommended_fix=guidance["recommended_fix"],
        location_label=guidance["location_label"],
        examples=[str(value) for value in _list(issue.get("examples"))],
    )


def _search_performance_section(external_seo_facts: JsonDict) -> SearchPerformanceSection:
    gsc = _dict(external_seo_facts.get("gsc"))
    url_inspection = _dict(external_seo_facts.get("url_inspection"))
    gsc_complete = gsc.get("status") == "complete"
    # "partial" means some per-URL inspections errored; the rows that DID succeed
    # are real Google answers and are still worth showing (scoring stays
    # complete-only, so partial data never affects the score).
    url_inspection_complete = url_inspection.get("status") in {"complete", "partial"}
    top_queries = (
        [_int_counts(_dict(row)) for row in _list(gsc.get("top_queries"))] if gsc_complete else []
    )
    top_pages = (
        [_int_counts(_dict(row)) for row in _list(gsc.get("top_pages"))] if gsc_complete else []
    )
    low_ctr_pages = (
        [_int_counts(_dict(row)) for row in _list(gsc.get("high_impression_low_ctr_pages"))]
        if gsc_complete
        else []
    )
    ranking_opportunities = (
        [_int_counts(_dict(row)) for row in _list(gsc.get("ranking_opportunities"))]
        if gsc_complete
        else []
    )
    declining_pages = (
        [_int_counts(_dict(row)) for row in _list(gsc.get("declining_pages"))]
        if gsc_complete
        else []
    )
    inspection_items = (
        [_inspection_item(_dict(item)) for item in _list(url_inspection.get("items"))]
        if url_inspection_complete
        else []
    )
    gsc_status = str(gsc.get("status") or "skipped")
    return SearchPerformanceSection(
        status=gsc_status,
        status_label=status_label(gsc_status),
        reason_label=_reason_label(gsc.get("reason") or gsc.get("error")),
        site_url=str(gsc.get("site_url")) if gsc.get("site_url") else None,
        date_range=_dict(gsc.get("date_range")) if gsc_complete else {},
        previous_date_range=_dict(gsc.get("previous_date_range")) if gsc_complete else {},
        summary=_dict(gsc.get("summary")) if gsc_complete else {},
        top_queries=top_queries,
        top_pages=top_pages,
        high_impression_low_ctr_pages=low_ctr_pages,
        ranking_opportunities=ranking_opportunities,
        declining_pages=declining_pages,
        url_inspection_summary=_dict(url_inspection.get("summary"))
        if url_inspection_complete
        else {},
        url_inspection_items=inspection_items,
        opportunity=_dict(gsc.get("opportunity")) if gsc_complete else {},
        branded=_dict(gsc.get("branded")) if gsc_complete else {},
        topic_clusters=[_dict(row) for row in _list(gsc.get("topic_clusters"))]
        if gsc_complete
        else [],
    )


def _inspection_item(item: JsonDict) -> JsonDict:
    """Add plain-language fields next to Google's raw URL Inspection values."""
    on_google = item.get("on_google")
    if on_google is True:
        on_google_label = "Yes"
    elif on_google is False:
        on_google_label = "No"
    else:
        on_google_label = "Unknown"
    return {**item, "on_google_label": on_google_label}


def _crawl_summary(crawled_pages: JsonDict) -> CrawlSummary:
    summary = _dict(crawled_pages.get("summary"))
    return CrawlSummary(
        status=str(crawled_pages.get("status") or "unknown"),
        successful_pages=int(summary.get("successful_pages") or 0),
        failed_pages=int(summary.get("failed_pages") or 0),
        skipped_pages=int(summary.get("skipped_pages") or 0),
        failed_page_items=[_dict(page) for page in _list(crawled_pages.get("failed_pages"))],
        skipped_page_items=[_dict(page) for page in _list(crawled_pages.get("skipped_pages"))],
    )


def _report_generated_at(result: Any) -> datetime:
    """Return a stable report-generation timestamp.

    Prefer the PDF renderer's recorded ``generated_at``, then the result row's
    ``created_at`` (collection completion), so the value does not drift every time
    the detail endpoint is polled. Only fall back to "now" when neither exists.
    """
    metadata = _dict(getattr(result, "report_metadata", None))
    raw = metadata.get("generated_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass

    created = getattr(result, "created_at", None)
    if isinstance(created, datetime):
        return created if created.tzinfo else created.replace(tzinfo=UTC)

    return datetime.now(UTC)


def _domain(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or url
    return hostname.removeprefix("www.")


def _accessibility_issue(issue: JsonDict) -> AccessibilityIssue:
    return AccessibilityIssue(
        rule_id=_text(issue.get("rule_id"), "unknown"),
        impact=_text(issue.get("impact"), "minor"),
        wcag_criteria=[str(value) for value in _list(issue.get("wcag_criteria"))],
        help=_text(issue.get("help"), ""),
        help_url=_text(issue.get("help_url"), ""),
        instances=int(issue.get("instances") or 0),
        example_selectors=[str(value) for value in _list(issue.get("example_selectors"))],
        example_pages=[str(value) for value in _list(issue.get("example_pages"))],
        failure_summary=_text(issue.get("failure_summary"), ""),
    )


def _accessibility_advisory_section(facts: JsonDict) -> AccessibilityAdvisorySection:
    """Compose the optional advisory accessibility section from the stored advisory facts.
    Pure presentation: it reads only ``accessibility_facts`` and never touches scores."""
    status = str(facts.get("status") or "skipped")
    if status != "complete":
        return AccessibilityAdvisorySection(status=status, status_label=status_label(status))
    impact_counts = {
        level: int(_dict(facts.get("impact_counts")).get(level) or 0)
        for level in ("critical", "serious", "moderate", "minor")
    }
    return AccessibilityAdvisorySection(
        status="complete",
        status_label=status_label("complete"),
        disclaimer=_text(facts.get("disclaimer"), ""),
        axe_version=_text(facts.get("axe_version"), "unknown"),
        pages_scanned=int(facts.get("pages_scanned") or 0),
        impact_counts=impact_counts,
        needs_review_count=int(facts.get("needs_review_count") or 0),
        issues=[_accessibility_issue(_dict(item)) for item in _list(facts.get("issues"))],
        notes=[str(note) for note in _list(facts.get("notes"))],
    )


def _dict(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, fallback: str) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned or fallback


def _severity(value: Any) -> Literal["info", "low", "medium", "high"]:
    return str(value) if value in {"info", "low", "medium", "high"} else "info"


def _tier(value: Any) -> RecommendationTier:
    return str(value) if value in TIER_LABELS else "quick_win"


def _rule_result(value: Any) -> Literal["pass", "partial", "fail", "skipped"]:
    return str(value) if value in {"pass", "partial", "fail", "skipped"} else "fail"


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _evidence_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return str(value)
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    if isinstance(value, dict):
        return f"{len(value)} field(s)"
    return str(value)
