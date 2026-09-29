from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import BaseModel, ConfigDict, Field

from apps.shared.config import Settings
from apps.worker.stages.report_payload import ReportPayload, compose_report_payload

JsonDict = dict[str, Any]
DOCX_RENDERER_VERSION = "phase1-docx-v1"
_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class DocxRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    docx_path: str
    report_metadata: JsonDict
    size_bytes: int = Field(ge=1)


def render_audit_docx(job: Any, result: Any, settings: Settings) -> DocxRenderResult:
    payload = compose_report_payload(job, result, settings=settings)
    output_path = _output_path(settings.local_report_storage_dir, str(job.id))
    return render_report_docx(payload, output_path=output_path)


def render_report_docx(payload: ReportPayload, *, output_path: Path) -> DocxRenderResult:
    rendered_at = datetime.now(UTC)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    files = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _package_relationships(),
        "docProps/core.xml": _core_properties(payload, rendered_at),
        "docProps/app.xml": _app_properties(),
        "word/document.xml": _document_xml(payload),
        "word/styles.xml": _styles_xml(),
        "word/_rels/document.xml.rels": _empty_relationships(),
    }

    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)

    size_bytes = output_path.stat().st_size
    return DocxRenderResult(
        docx_path=str(output_path),
        report_metadata={
            "status": "complete",
            "renderer": "docx_ooxml",
            "renderer_version": DOCX_RENDERER_VERSION,
            "generated_at": rendered_at.isoformat(),
            "docx_path": str(output_path),
            "docx_size_bytes": size_bytes,
        },
        size_bytes=size_bytes,
    )


# Replaces the "How to use it" advice when the payload is a teaser (report_profile.py).
_TEASER_SECTION_GUIDE = (
    "What happens next: Each finding shows what the audit found and why it matters. The "
    "fixes, in priority order, are covered in a short meeting (see Next Steps)."
)


def _document_xml(payload: ReportPayload) -> str:
    parts: list[str] = []
    teaser = payload.report_profile == "teaser"
    metadata = payload.metadata
    # The one shared combined predicate (computed in compose_report_payload): "website_only"
    # (failed social collection) still carries a score; the title must not promise a social
    # section the document doesn't have.
    is_combined = bool(payload.combined_complete)
    parts.append(
        _paragraph(
            "Website & Social Media Audit Report" if is_combined else "Website Audit Report",
            "Title",
        )
    )
    parts.append(_paragraph(metadata.final_url, "Subtitle"))
    parts.append(_paragraph(f"Generated: {metadata.generated_date}", "Meta"))
    parts.append(_paragraph(f"Pages reviewed: {metadata.pages_crawled}", "Meta"))
    if metadata.niche:
        parts.append(_paragraph(f"Niche: {metadata.niche}", "Meta"))
    if metadata.target_audience:
        parts.append(_paragraph(f"Audience: {metadata.target_audience}", "Meta"))

    parts.append(_heading("Executive Summary", 1))
    parts.append(_paragraph(payload.executive_summary))
    for score in payload.scores:
        parts.append(_paragraph(f"{score.label}: {score.score}/{score.max_score}", "Strong"))
        parts.append(_paragraph(score.description))

    scope = payload.website_scope if isinstance(payload.website_scope, dict) else None
    if scope:
        parts.append(_heading("What Your Website Consists Of", 1))
        parts.append(
            _paragraph(
                "A snapshot of the whole site the audit discovered — pages, posts, sitemap, "
                "outbound links, and images. Discovered counts come from the site's internal links "
                "plus its sitemap; the audit analyzes the most important pages in depth."
            )
        )
        for label, key in (
            ("Pages discovered", "pages_discovered"),
            ("Pages analyzed in depth", "pages_analyzed"),
            ("Blog / article posts", "blog_posts"),
            ("Sitemap entries", "sitemap_entries"),
            ("Outbound links", "outbound_links"),
            ("Images", "images"),
        ):
            value = scope.get(key)
            if value is not None:
                parts.append(_paragraph(f"{label}: {value}", "Meta"))

    for section in payload.sections:
        parts.append(_heading(section.label, 1))
        if section.score is not None:
            parts.append(_paragraph(f"Score: {section.score}/100", "Strong"))
        parts.append(_paragraph(section.headline))
        if section.id == "seo":
            parts.append(
                _paragraph(
                    "How to read this section: SEO findings explain what may keep the site "
                    "from being found, understood, or clicked in search results."
                )
            )
            parts.append(
                _paragraph(
                    _TEASER_SECTION_GUIDE
                    if teaser
                    else "How to use it: Turn each recommendation into a site update, then "
                    "rerun the audit to confirm the same issue no longer appears."
                )
            )
        elif section.id == "uxui":
            parts.append(
                _paragraph(
                    "How to read this section: UX/UI findings explain what may make it harder "
                    "for visitors to trust the page, understand the offer, or take the next step."
                )
            )
            parts.append(
                _paragraph(
                    _TEASER_SECTION_GUIDE
                    if teaser
                    else "How to use it: Prioritize changes that make the primary action "
                    "easier to see, easier to trust, and easier to complete."
                )
            )

        if section.findings:
            parts.append(_heading("Findings", 2))
            # One entry per issue: the fix travels with its finding; the roadmap
            # re-groups the same fixes by timeline (no separate recommendations list).
            for finding in section.findings:
                tier_label = finding.tier.replace("_", " ").title() if finding.tier else ""
                title_line = f"{finding.severity.upper()}: {finding.title}"
                if tier_label:
                    title_line = f"{title_line} ({tier_label})"
                parts.append(_paragraph(title_line, "Strong"))
                parts.append(_paragraph(finding.explanation))
                for item in finding.action_items:
                    parts.append(_paragraph(f"Do this: {item}"))
        if section.show_recommendations:
            # Legacy stored audits (pre finding-card commentary) have no action_items on
            # their findings — their fixes live only in the per-section recommendations.
            # compose_report_payload computes the one shared show_recommendations flag so
            # this DOCX, the PDF, and the web UI can never disagree about the fallback.
            parts.append(_heading("Recommendations", 2))
            for recommendation in section.recommendations:
                tier_label = recommendation.tier.replace("_", " ").title()
                parts.append(_paragraph(f"{tier_label}: {recommendation.title}", "Strong"))
                parts.append(_paragraph(recommendation.rationale))
                for item in recommendation.action_items:
                    parts.append(_paragraph(item))

    parts.extend(_external_seo_xml(payload))

    if teaser and payload.cta is not None:
        parts.append(_heading("Next Steps", 1))
        parts.append(_paragraph(payload.cta.message))
        parts.append(_paragraph(payload.cta.label, "Strong"))
        if payload.cta.url:
            parts.append(_paragraph(payload.cta.url))
    else:
        parts.append(_heading("Lead Generation Roadmap", 1))
        for tier in payload.roadmap:
            parts.append(_heading(tier.label, 2))
            if not tier.recommendations:
                parts.append(_paragraph("No recommendations generated for this tier."))
                continue
            for recommendation in tier.recommendations:
                parts.append(_paragraph(recommendation.title, "Strong"))
                for item in recommendation.action_items:
                    parts.append(_paragraph(item))

    parts.append(_heading("Appendix", 1))
    parts.append(_paragraph(payload.appendix.scoring_note))
    parts.append(
        _paragraph(
            "PageSpeed (how fast Google considers the pages, 0-100): "
            f"{payload.pagespeed_summary.status_label}; analyzed "
            f"{payload.pagespeed_summary.pages_analyzed}/"
            f"{payload.pagespeed_summary.pages_requested} pages.",
            "Meta",
        )
    )
    cwv = payload.core_web_vitals
    if cwv.available:
        parts.append(_heading("Core Web Vitals", 2))
        parts.append(
            _paragraph(
                "Lab metrics below measure the homepage only; the PageSpeed score above "
                "averages every analyzed page.",
                "Meta",
            )
        )
        for row in cwv.lab_rows:
            mobile = (
                f"{row.mobile.value_label} ({row.mobile.rating_label})" if row.mobile else "N/A"
            )
            desktop = (
                f"{row.desktop.value_label} ({row.desktop.rating_label})" if row.desktop else "N/A"
            )
            parts.append(_paragraph(f"{row.label} — Mobile: {mobile}; Desktop: {desktop}.", "Meta"))
        if cwv.field_available:
            scope = f" ({cwv.field_source}, {cwv.field_form_factor})" if cwv.field_source else ""
            assessment = ""
            if cwv.field_assessment:
                assessment = f" Overall assessment: {cwv.field_assessment}."
            parts.append(
                _paragraph(f"Real-user field data (Chrome UX Report){scope}.{assessment}", "Meta")
            )
            for metric in cwv.field_metrics:
                parts.append(
                    _paragraph(
                        f"{metric.label} (75th percentile): "
                        f"{metric.value_label} ({metric.rating_label}).",
                        "Meta",
                    )
                )

    parts.append(
        _paragraph(
            "Fact check: every number in the written commentary was checked against the "
            "data collected from the site before this report was generated "
            f"({payload.validation_summary.numeric_claims_checked} numbers checked, "
            f"{payload.validation_summary.unsupported_claim_count} unverified and removed).",
            "Meta",
        )
    )

    # Combined audit only: append the Social Media Audit + Overall Lead-Gen Readiness sections at
    # the VERY END, mirroring the PDF. A website-only audit leaves these None, so the DOCX above
    # is unchanged (byte-identical to before).
    parts.extend(_combined_xml(payload))

    body = "".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}{_section_properties()}</w:body>"
        "</w:document>"
    )


def _external_seo_xml(payload: ReportPayload) -> list[str]:
    parts: list[str] = []
    technical = payload.technical_seo_section
    search = payload.search_performance_section
    technical_available = technical.status == "complete"
    search_available = search.status == "complete"

    parts.append(_heading("Site Health", 1))
    parts.append(
        _paragraph(
            f"Status: {technical.status_label}"
            + (f" (source: {technical.tool_label})" if technical.tool_label else "")
            + "; pages analyzed: "
            f"{technical.summary.get('urls_crawled', 'N/A') if technical_available else 'N/A'}.",
            "Meta",
        )
    )
    for note in technical.notes:
        parts.append(_paragraph(f"Coverage note: {note}", "Meta"))
    parts.append(
        _paragraph(
            "What this section tells you: This is the site health check for search engines. "
            "It flags broken links, blocked pages, missing page labels, duplicate metadata, "
            "and image text gaps that can stop Google or visitors from getting the right page."
        )
    )
    parts.append(
        _paragraph(
            "Why it matters: High-severity issues are the ones most likely to cost visitors, "
            "especially on important service, location, or lead pages."
            if payload.report_profile == "teaser"
            else "How to use it: Fix high-severity site health issues first, especially errors "
            "on important service, location, or lead pages. Use the example URLs as the "
            "starting list for the web team or CMS editor."
        )
    )
    if technical_available and technical.issues:
        for issue in technical.issues:
            parts.append(
                _paragraph(
                    f"{issue.severity.upper()}: {issue.title} ({issue.count})",
                    "Strong",
                )
            )
            parts.append(
                _paragraph(
                    " ".join(
                        part
                        for part in (issue.summary, issue.why_it_matters, issue.recommended_fix)
                        if part
                    )
                )
            )
            if issue.examples:
                shown = issue.examples[:4]
                if issue.count > len(shown):
                    parts.append(
                        _paragraph(f"Examples ({len(shown)} of {issue.count} affected):", "Meta")
                    )
                else:
                    parts.append(_paragraph("Affected URLs:", "Meta"))
                for example in shown:
                    parts.append(_bullet(example))
    elif technical_available:
        parts.append(
            _paragraph(
                "The site health check completed for this audit and did not find issue "
                "groups that matched the report thresholds."
            )
        )
    else:
        reason = f" Reason: {technical.reason_label}" if technical.reason_label else ""
        parts.append(
            _paragraph(
                "Technical site health data was not available for this report, so this section "
                f"does not make clean-or-broken technical SEO claims.{reason}"
            )
        )

    # Search Console blocks render only when the edition runs with it.
    if not payload.show_search_console:
        return parts

    parts.append(_heading("Google Search Performance", 1))
    parts.append(
        _paragraph(
            f"Status: {search.status_label}; property: {search.site_url or 'N/A'}.",
            "Meta",
        )
    )
    if search.date_range.get("start"):
        prev = search.previous_date_range
        prev_text = (
            f", compared with the preceding {prev.get('days')} days "
            f"({prev.get('start')} to {prev.get('end')})"
            if prev.get("start") and prev.get("days")
            else ""
        )
        # Results stored before the window facts existed have no "days" key; render the
        # bare range rather than "(None days)" in a client-facing document.
        days = search.date_range.get("days")
        days_text = f" ({days} days)" if days else ""
        parts.append(
            _paragraph(
                f"Data window: {search.date_range.get('start')} to "
                f"{search.date_range.get('end')}{days_text}"
                f"{prev_text}. Table figures are totals over this window.",
                "Meta",
            )
        )
    parts.append(
        _paragraph(
            "What this section tells you: Search Console shows how people already find the "
            "site in Google. It highlights pages with visibility but weak clicks, search terms "
            "close to stronger rankings, and pages losing traffic."
        )
    )
    parts.append(
        _paragraph(
            "How to read the numbers: Impressions = how many times the page appeared in "
            "Google results. Clicks = how many times searchers chose it. CTR (click-through "
            "rate) = clicks divided by impressions. Position = the average ranking spot in "
            "the results (1 is the top)."
        )
    )
    if search_available:
        _append_search_rows(
            parts,
            "High-impression, low-CTR pages",
            search.high_impression_low_ctr_pages,
            "page",
        )
        _append_search_rows(
            parts,
            "Queries ranking in positions 4-20",
            search.ranking_opportunities,
            "query",
        )
        _append_search_rows(parts, "Declining pages", search.declining_pages, "page")

    if search.url_inspection_items:
        parts.append(_heading("URL Inspection", 2))
        parts.append(
            _paragraph(
                "This asks Google directly whether each inspected page is in its index — "
                "the homepage plus the most prominent pages found during the review (up "
                "to 20). 'On Google' is Google's answer; the status text is Google's own "
                "wording for how it handled the page.",
                "Meta",
            )
        )
        for item in search.url_inspection_items[:8]:
            parts.append(
                _bullet(
                    f"{item.get('url')}: On Google: {item.get('on_google_label') or 'Unknown'}; "
                    f"Google's status: {item.get('coverage_state') or item.get('status')}"
                )
            )
    elif not search_available:
        parts.append(
            _paragraph(
                "Connect a verified Search Console property and rerun enrichment to populate "
                "Google search performance insights."
            )
        )
    elif (
        not search.high_impression_low_ctr_pages
        and not search.ranking_opportunities
        and not search.declining_pages
    ):
        parts.append(
            _paragraph(
                "Search Console completed for this audit and did not return ranking, CTR, "
                "or decline opportunities that matched the report thresholds."
            )
        )

    return parts


def _append_search_rows(parts: list[str], title: str, rows: list[JsonDict], label_key: str) -> None:
    if not rows:
        return
    parts.append(_heading(title, 2))
    for row in rows[:8]:
        label = row.get(label_key) or row.get("page") or row.get("query") or "Unknown"
        parts.append(
            _bullet(
                f"{label}: clicks={row.get('clicks', row.get('current_clicks', 0))}, "
                f"impressions={row.get('impressions', row.get('current_impressions', 0))}, "
                f"position={row.get('position', 'N/A')}"
            )
        )


def _combined_xml(payload: ReportPayload) -> list[str]:
    """Combined-audit social + overall + benchmark + AI-visibility sections, appended at the end of
    the website DOCX. Returns [] for a website-only audit (social_audit/overall_readiness/benchmark/
    ai_visibility all None), leaving the DOCX unchanged. Carries the same social depth as the PDF —
    quantified findings, strengths, content insights, and top posts — as headings + bullets (no
    table machinery)."""
    parts: list[str] = []
    social = payload.social_audit
    overall = payload.overall_readiness
    benchmark = payload.benchmark
    ai_visibility = payload.ai_visibility

    if social:
        parts.append(_heading("Social Media Audit", 1))
        score = social.get("score")
        score_text = (
            f"Social Score: {score}/100" if score is not None else "Social Score: not available"
        )
        parts.append(_paragraph(score_text, "Strong"))
        parts.append(
            _paragraph(
                "A standalone audit of the brand's public social profiles - profile completeness, "
                "posting cadence, engagement, and lead-capture signals - scored independently of "
                "the website."
            )
        )
        status = social.get("status")
        if status not in ("complete", "partial"):
            parts.append(
                _paragraph(
                    f"Social profiles could not be collected for this audit ({status}), so the "
                    "social score is unavailable."
                )
            )
        else:
            # Prefer the curated per_platform scorecard projection (the same source the PDF
            # table and the web UI consume). The raw-platforms fallback is purely defensive:
            # payloads are composed live by build_social_report_data (never stored), so
            # per_platform is always present today.
            platform_rows = social.get("per_platform") or social.get("platforms")
            platforms = [p for p in (platform_rows or []) if isinstance(p, dict)]
            if platforms:
                parts.append(_heading("Profiles audited", 2))
                for p in platforms:
                    parts.append(_bullet(_social_platform_line(p)))
            findings = [f for f in (social.get("findings") or []) if isinstance(f, dict)]
            if findings:
                parts.append(
                    _heading(
                        "What we found"
                        if payload.report_profile == "teaser"
                        else "What to improve",
                        2,
                    )
                )
                for f in findings:
                    label = f.get("label", "")
                    if f.get("metric"):
                        # Em-dash, not parens: the metric already contains a parenthesised
                        # target ("0.0% (target ≥ 2%)") — wrapping would nest parentheses.
                        label = f"{label} — {f.get('metric')}"
                    parts.append(
                        _paragraph(f"{str(f.get('impact', 'medium')).upper()}: {label}", "Strong")
                    )
                    if f.get("remediation"):
                        parts.append(_paragraph(f.get("remediation")))
            else:
                parts.append(
                    _paragraph(
                        "No social findings were surfaced - the audited profiles met the "
                        "rubric's checks."
                    )
                )
            strengths = [s for s in (social.get("strengths") or []) if isinstance(s, dict)]
            if strengths:
                parts.append(_heading("What's working", 2))
                for s in strengths:
                    parts.append(_bullet(s.get("label", "")))
            insight_lines = _social_insight_lines(social.get("content_insights"))
            if insight_lines:
                parts.append(_heading("Content insights", 2))
                for line in insight_lines:
                    parts.append(_bullet(line))
            google_business = social.get("google_business")
            if isinstance(google_business, dict) and any(
                value is not None for value in google_business.values()
            ):
                # The listing the Google-reviews and phone (NAP) checks were scored against —
                # shown so the reader can verify the right business was matched.
                parts.append(_heading("Google Business Profile", 2))
                name = google_business.get("name")
                if name:
                    category = google_business.get("category")
                    parts.append(_bullet(f"{name} ({category})" if category else str(name)))
                rating_line = google_business.get("rating_line")
                if rating_line:
                    # Precomposed by build_social_report_data — the one source the PDF uses too.
                    parts.append(_bullet(str(rating_line)))
                for key, label in (
                    ("address", "Address"),
                    ("phone", "Phone"),
                    ("website", "Website"),
                ):
                    value = google_business.get(key)
                    if value:
                        parts.append(_bullet(f"{label}: {value}"))
            connected_youtube = social.get("connected_youtube")
            if isinstance(connected_youtube, dict) and connected_youtube.get("lines"):
                # Lines are precomposed by build_social_report_data — the one source the PDF
                # and the web UI use too, so the three surfaces can't drift.
                parts.append(_heading("Connected YouTube analytics", 2))
                meta = connected_youtube.get("meta")
                if meta:
                    parts.append(_paragraph(str(meta), "Meta"))
                for line in connected_youtube["lines"]:
                    parts.append(_bullet(str(line)))
            top_posts = [tp for tp in (social.get("top_posts") or []) if isinstance(tp, dict)]
            if top_posts:
                parts.append(_heading("Top performing posts", 2))
                for tp in top_posts:
                    views = tp.get("views")
                    parts.append(
                        _bullet(
                            f"{tp.get('platform', '-')}: "
                            f"{tp.get('title') or tp.get('posted') or '-'} - "
                            f"views={views if views is not None else '-'}, "
                            f"engagement={tp.get('engagement', 0)}"
                        )
                    )

    if overall and overall.get("score") is not None:
        weights = overall.get("weights") or {}
        inputs = overall.get("inputs") or {}
        web = inputs.get("website_lead_gen")
        soc = inputs.get("social")
        parts.append(_heading("Overall Lead-Gen Readiness", 1))
        parts.append(
            _paragraph(f"Overall Lead-Gen Readiness: {overall.get('score')}/100", "Strong")
        )
        parts.append(
            _paragraph(
                "A single headline score combining the website audit (SEO + UX/UI) and the social "
                "media audit, weighted toward the website because that is where leads are captured."
            )
        )
        # Half-up like the UI's Math.round (project convention int(x+0.5)) — Python's round()
        # is banker's rounding, and a 0.705/0.295 rubric would print 70% here vs 71% in the UI.
        website_weight_pct = int(float(weights.get("website", 0)) * 100 + 0.5)
        social_weight_pct = int(float(weights.get("social", 0)) * 100 + 0.5)
        parts.append(
            _paragraph(
                f"Website Lead-Gen (SEO + UX/UI): {web if web is not None else '-'} "
                f"(weight {website_weight_pct}%).",
                "Meta",
            )
        )
        parts.append(
            _paragraph(
                f"Social Media: {soc if soc is not None else '-'} (weight {social_weight_pct}%).",
                "Meta",
            )
        )
        if overall.get("status") == "website_only":
            parts.append(
                _paragraph(
                    "Social data was unavailable, so this score reflects the website audit only."
                )
            )

    if benchmark and benchmark.get("competitors"):
        parts.append(_heading("Competitor Benchmarking", 1))
        parts.append(
            _paragraph(
                "How this site's scores compare to competitor and industry baselines. Benchmarks "
                "are presentation only and do not change any audit score."
            )
        )
        for competitor in benchmark.get("competitors") or []:
            if not isinstance(competitor, dict):
                continue
            label = competitor.get("label", "-")
            if competitor.get("is_industry"):
                label = f"{label} (industry baseline)"
            parts.append(_heading(label, 2))
            for m in competitor.get("metrics") or []:
                if not isinstance(m, dict):
                    continue
                # Presentation strings (delta_display / verdict_label) come pre-formatted from
                # build_benchmark_report_data, so PDF and DOCX stay in lockstep.
                parts.append(
                    _bullet(
                        f"{m.get('label', m.get('metric', '-'))}: this site "
                        f"{m.get('your_score', '-')} vs baseline {m.get('baseline', '-')} "
                        f"({m.get('delta_display', '-')}, {m.get('verdict_label', '-')})"
                    )
                )
        provider = benchmark.get("provider")
        if provider:
            parts.append(_paragraph(f"Benchmark source: {provider}.", "Meta"))

    if ai_visibility and ai_visibility.get("unavailable"):
        # A blocked run (CAPTCHA / login wall) — show the honest note, not the data tables.
        parts.append(_heading("AI Visibility", 1))
        parts.append(
            _paragraph(
                ai_visibility.get("message")
                or "AI Visibility data could not be retrieved for this report."
            )
        )
    elif ai_visibility:
        parts.append(_heading("AI Visibility", 1))
        parts.append(
            _paragraph(
                "How this brand appears in AI-generated answers (ChatGPT, Google AI Overviews / AI "
                "Mode, Gemini, Perplexity), from the Semrush AI Visibility Toolkit. Presentation "
                "only - it does not change any audit score. This measures brand mentions in AI "
                "answers to a tracked sample of prompts - it is not website traffic, so analytics "
                "can still record visits from AI tools (organic referrals or AI ads) even when "
                "tracked visibility is near zero."
            )
        )
        score = ai_visibility.get("visibility_score")
        if score is not None:
            band = ai_visibility.get("visibility_band")
            suffix = f" - {band}" if band else ""
            parts.append(_paragraph(f"AI Visibility Score: {score}/100{suffix}", "Strong"))
        for m in ai_visibility.get("metrics") or []:
            if isinstance(m, dict):
                parts.append(_bullet(f"{m.get('label', '-')}: {m.get('value', '-')}"))

        per_platform = [p for p in (ai_visibility.get("per_platform") or []) if isinstance(p, dict)]
        if per_platform:
            parts.append(_heading("Distribution by AI platform", 2))
            for p in per_platform:
                mentions = p.get("mentions")
                share = p.get("share_display")
                parts.append(
                    _bullet(
                        f"{p.get('platform', '-')}: "
                        f"mentions={mentions if mentions is not None else '-'}, "
                        f"share={share if share else '-'}"
                    )
                )

        topics = [t for t in (ai_visibility.get("topics") or []) if isinstance(t, dict)]
        if topics:
            parts.append(_heading("Top performing topics", 2))
            for t in topics:
                vis = t.get("visibility")
                mine = t.get("your_mentions")
                parts.append(
                    _bullet(
                        f"{t.get('topic', '-')}: "
                        f"visibility={vis if vis is not None else '-'}, "
                        f"your mentions={mine if mine is not None else '-'}, "
                        f"AI volume={t.get('ai_volume') or '-'}"
                    )
                )

        competitors = [c for c in (ai_visibility.get("competitors") or []) if isinstance(c, dict)]
        if competitors:
            parts.append(_heading("Competitors in AI answers", 2))
            for c in competitors:
                vis = c.get("visibility_score")
                mentions = c.get("mentions")
                parts.append(
                    _bullet(
                        f"{c.get('label', '-')}: "
                        f"visibility={vis if vis is not None else '-'}, "
                        f"mentions={mentions if mentions is not None else '-'}"
                    )
                )

        by_country = [c for c in (ai_visibility.get("by_country") or []) if isinstance(c, dict)]
        if by_country:
            parts.append(_heading("Mentions by country", 2))
            for c in by_country:
                mentions = c.get("mentions")
                share = c.get("share_display")
                parts.append(
                    _bullet(
                        f"{c.get('country', '-')}: "
                        f"mentions={mentions if mentions is not None else '-'}, "
                        f"share={share if share else '-'}"
                    )
                )

        retrieved = ai_visibility.get("retrieved_at")
        source = "Source: Semrush AI Visibility Toolkit"
        if retrieved:
            source = f"{source}, retrieved {retrieved}"
        parts.append(_paragraph(f"{source}.", "Meta"))

    return parts


def _social_platform_line(p: JsonDict) -> str:
    """One per-platform scorecard bullet, extended with the video-share/business columns the
    PDF's scorecard table shows (only when present)."""
    followers = p.get("followers")
    ppm = p.get("posts_per_month")
    eng = p.get("avg_engagement_rate_pct")
    last = p.get("days_since_last_post")
    # `or '-'`, not .get(default): per_platform always SETS these keys (possibly to None), so a
    # dict default never fires and a handle-less profile would render "(@None)".
    handle = p.get("handle") or "-"
    line = (
        f"{p.get('platform') or '-'} (@{handle}): "
        f"followers={followers if followers is not None else '-'}, "
        f"posts/mo={ppm if ppm is not None else '-'}, "
        f"engagement={f'{eng}%' if eng is not None else '-'}, "
        f"last post={f'{last}d ago' if last is not None else '-'}"
    )
    if p.get("video_share_pct") is not None:
        line += f", video share={p.get('video_share_pct')}%"
    if p.get("is_business") is not None:
        line += f", business={'yes' if p.get('is_business') else 'no'}"
    return line


def _social_insight_lines(insights: Any) -> list[str]:
    """Bullet-ready content-insight lines covering the same non-None field set the PDF renders."""
    ci = insights if isinstance(insights, dict) else {}
    lines: list[str] = []
    mix = ci.get("content_mix") if isinstance(ci.get("content_mix"), dict) else {}
    mix_parts = [
        f"{label} {value}%"
        for label, value in (
            ("video", mix.get("video")),
            ("image", mix.get("image")),
            ("carousel", mix.get("carousel")),
        )
        if value is not None
    ]
    if mix_parts:
        lines.append("Content mix: " + ", ".join(mix_parts))
    for label, key, suffix in (
        ("Lifetime YouTube views", "total_views", ""),
        ("Avg views per video", "avg_views_per_post", ""),
        ("Avg engagement rate", "avg_engagement_rate_pct", "%"),
        ("Likes per comment", "avg_like_to_comment_ratio", ""),
        ("Hashtags per post", "avg_hashtags_per_post", ""),
        ("Captions with a CTA", "posts_with_cta_caption_pct", "%"),
        ("Longest posting gap", "max_posting_gap_days", " days"),
        ("Follower/following ratio", "avg_follower_following_ratio", "x"),
    ):
        value = ci.get(key)
        if value is not None:
            lines.append(f"{label}: {value}{suffix}")
    return lines


def _paragraph(text: Any, style: str | None = None) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t>{_xml_text(text)}</w:t></w:r></w:p>"


def _heading(text: Any, level: int) -> str:
    return _paragraph(text, f"Heading{min(max(level, 1), 2)}")


def _bullet(text: Any) -> str:
    return _paragraph(f"- {text}", "ListParagraph")


def _section_properties() -> str:
    return (
        "<w:sectPr>"
        '<w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" '
        'w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr>"
    )


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="22"/></w:rPr>
    <w:pPr><w:spacing w:after="140"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="1F74B7"/><w:sz w:val="44"/></w:rPr>
    <w:pPr><w:spacing w:after="220"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:color w:val="5C697A"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="1F74B7"/><w:sz w:val="32"/></w:rPr>
    <w:pPr><w:spacing w:before="300" w:after="160"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="28864B"/><w:sz w:val="26"/></w:rPr>
    <w:pPr><w:spacing w:before="220" w:after="120"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Strong">
    <w:name w:val="Strong"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:b/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Meta">
    <w:name w:val="Meta"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:color w:val="5C697A"/><w:sz w:val="19"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph">
    <w:name w:val="List Paragraph"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="360"/></w:pPr>
  </w:style>
</w:styles>"""


def _content_types() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'extended-properties+xml"/>'
        "</Types>"
    )


def _package_relationships() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/'
        'metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _empty_relationships() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )


def _core_properties(payload: ReportPayload, rendered_at: datetime) -> str:
    title = _xml_text(f"{payload.metadata.site_domain} Website Audit Report")
    timestamp = rendered_at.isoformat()
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{title}</dc:title>"
        "<dc:creator>BLC Website Audit</dc:creator>"
        "<cp:lastModifiedBy>BLC Website Audit</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _app_properties() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
  xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>BLC Website Audit</Application>
</Properties>"""


def _xml_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = _INVALID_XML_CHARS.sub("", text)
    return escape(text, quote=False)


def _output_path(storage_dir: Path, audit_id: str) -> Path:
    return (storage_dir / f"{audit_id}.docx").resolve()
