# flake8: noqa: E501
# ruff: noqa: E501
"""Deterministic content plan for audit commentary.

This is the canonical source of truth for what a report *says*: which findings exist,
in what order, their severity and remediation tier, and the baseline prose for each.
It is a pure function of the score breakdown + extracted facts, so the same site always
produces the same findings and recommendations - with or without an LLM.

An optional LLM polish layer (Phase 2) may later rewrite the prose strings, but it can
never add, drop, reorder, or invent a finding. See docs/RUBRICS.md §8.

Grounding-safety invariant: every number emitted here is either a section/composite score
or a rule's ``evidence.value`` (the resolved fact at its ``fact_path``). Both are present
in the grounding fact sources, so the deterministic baseline is never stripped. Do not
emit derived/aggregate numbers that are not stored facts.
"""

from __future__ import annotations

from typing import Any

from apps.shared.config import Settings
from apps.worker.stages.commentary import (
    CommentaryContent,
    CommentaryFinding,
    CommentaryRecommendation,
    CommentarySection,
)
from apps.worker.stages.scoring import resolve_fact_path

JsonDict = dict[str, Any]

Severity = str  # one of: high, medium, low, info
Tier = str  # one of: quick_win, mid_term, long_term

SECTION_TITLES = {"seo": "SEO", "uxui": "UX/UI"}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "info": 0}
TIER_ORDER = {"quick_win": 0, "mid_term": 1, "long_term": 2}

# Finding severity = rule impact x evaluated result.
_SEVERITY_MATRIX: dict[tuple[str, str], Severity] = {
    ("high", "fail"): "high",
    ("high", "partial"): "medium",
    ("medium", "fail"): "medium",
    ("medium", "partial"): "low",
    ("low", "fail"): "low",
    ("low", "partial"): "info",
}

_DEFAULT_ACTION = "Address this item to improve the score."


def build_content_plan(
    *,
    audit_context: JsonDict,
    seo_facts: JsonDict,
    uxui_facts: JsonDict,
    psi_facts: JsonDict,
    external_seo_facts: JsonDict | None = None,
    score_breakdown: JsonDict,
    settings: Settings,
) -> CommentaryContent:
    """Build the deterministic commentary plan from the score breakdown.

    ``*_facts`` are accepted for symmetry with the LLM path (and future templates that
    may cite additional stored facts); the current templates draw their numbers from the
    rule ``evidence.value`` already embedded in ``score_breakdown``.
    """
    max_findings = settings.commentary_max_findings_per_section
    facts = {
        "seo": seo_facts,
        "uxui": uxui_facts,
        "psi": psi_facts,
        "external_seo": external_seo_facts or {},
    }

    seo_section = _build_section("seo", score_breakdown, facts, max_findings)
    uxui_section = _build_section("uxui", score_breakdown, facts, max_findings)

    scores = _dict(score_breakdown.get("scores"))
    top_label = _top_priority_label(score_breakdown)
    # Business-opportunity framing (P1): when Search Console is connected, the GSC stage stores a
    # deterministic "clicks/leads left on the table" estimate. Every number it carries lives in the
    # external_seo facts, so the grounding validator keeps the prose that cites it.
    opportunity = _dict(_dict(_dict(external_seo_facts).get("gsc")).get("opportunity"))

    return CommentaryContent(
        executive_summary=_executive_summary(scores, top_label, opportunity),
        seo=seo_section,
        uxui=uxui_section,
        lead_generation=_lead_section(scores),
    )


def _build_section(
    section_id: str,
    score_breakdown: JsonDict,
    facts: JsonDict,
    max_findings: int,
) -> CommentarySection:
    surfaced = _merge_overlapping_rules(_surfaced_rules(section_id, score_breakdown))
    score = _int(_dict(score_breakdown.get("scores")).get(section_id))

    finding_order = sorted(surfaced, key=_finding_sort_key)
    selected = finding_order[:max_findings]
    findings = [_finding(rule, facts) for rule in selected]

    # A rendered finding without its fix reads as an unanswered problem (a tier-first
    # sort used to push long_term fixes like PageSpeed past the cap), so recommendations
    # are the SAME selected rules, ordered for display by tier — the findings cap is the
    # only truncation knob.
    rec_order = sorted(selected, key=_recommendation_sort_key)
    recommendations = [_recommendation(rule, facts) for rule in rec_order]

    label = SECTION_TITLES.get(section_id, section_id.upper())
    return CommentarySection(
        headline=f"{label} score is {score}",
        findings=findings,
        recommendations=recommendations,
    )


def _merge_overlapping_rules(surfaced: list[JsonDict]) -> list[JsonDict]:
    """Fold secondary rules into their surfaced primary; each alone still surfaces.

    Which rule folds into which is the rubric's ``merged_into`` metadata (carried per rule
    in the score breakdown) — the relation lives in rubrics/*.yaml next to the rule ids it
    names, and a dangling target fails rubric load. Presentation-level only; scores
    untouched. Pre-v12 stored breakdowns simply have no ``merged_into`` keys, so their
    stored commentary renders exactly as it was authored.

    The merged card adopts the strongest severity and weight in its group: a ``fail``
    secondary folded into a ``partial`` primary must not sink the combined card below
    the findings cap (the issue would vanish from the report) or display a softer
    severity than the check it absorbed.
    """
    surfaced_ids = {str(rule.get("rule_id") or "") for rule in surfaced}
    covered: dict[str, list[JsonDict]] = {}
    kept: list[JsonDict] = []
    for rule in surfaced:
        rule_id = str(rule.get("rule_id") or "")
        primary_id = str(rule.get("merged_into") or "")
        if primary_id and primary_id in surfaced_ids:
            covered.setdefault(primary_id, []).append(rule)
            continue
        kept.append(rule)
    if not covered:
        return kept
    merged: list[JsonDict] = []
    for rule in kept:
        rule_id = str(rule.get("rule_id") or "")
        secondaries = covered.get(rule_id)
        if secondaries:
            rule = dict(rule)
            rule["covers_related"] = "; ".join(
                str(sec.get("finding_label") or sec.get("description") or sec.get("rule_id"))
                for sec in secondaries
            )
            group = [rule, *secondaries]
            rule["severity_override"] = max(
                (_rule_severity(member) for member in group),
                key=lambda severity: SEVERITY_RANK[severity],
            )
            rule["weight"] = max(_float(member.get("weight")) for member in group)
        merged.append(rule)
    # Defensive: a covered group whose primary never made it into `kept` (malformed merge
    # metadata in a stored breakdown — a self-merge or a chain, which rubric load now
    # rejects but old stored data could still carry) must not vanish from the report;
    # re-keep those rules unmerged.
    kept_ids = {str(rule.get("rule_id") or "") for rule in kept}
    for primary_id, secondaries in covered.items():
        if primary_id not in kept_ids:
            merged.extend(secondaries)
    return merged


def _with_covered_note(rule: JsonDict, why: str) -> str:
    """Append the merged secondary check(s) so the single card covers both (number-free
    so the grounding validator never strips it)."""
    covered = rule.get("covers_related")
    if not covered:
        return why
    return f"{why} Fixing this also resolves a related check: {covered}.".strip()


def _surfaced_rules(section_id: str, score_breakdown: JsonDict) -> list[JsonDict]:
    category = _dict(_dict(score_breakdown.get("categories")).get(section_id))
    rules = category.get("rules")
    if not isinstance(rules, list):
        return []
    return [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("result") in {"fail", "partial"}
        and rule.get("surface_as_finding", True)
    ]


def _rule_severity(rule: JsonDict) -> Severity:
    """Severity for ranking/display; merged cards carry their group's strongest severity."""
    override = rule.get("severity_override")
    if isinstance(override, str) and override in SEVERITY_RANK:
        return override  # type: ignore[return-value]
    return _severity(rule.get("impact"), rule.get("result"))


def _finding_sort_key(rule: JsonDict) -> tuple[int, float, str]:
    severity = _rule_severity(rule)
    return (-SEVERITY_RANK[severity], -_float(rule.get("weight")), str(rule.get("rule_id") or ""))


def _recommendation_sort_key(rule: JsonDict) -> tuple[int, int, float, str]:
    tier_rank = TIER_ORDER[_tier(rule.get("tier"))]
    severity_rank, neg_weight, rule_id = _finding_sort_key(rule)
    return (tier_rank, severity_rank, neg_weight, rule_id)


def _rule_action(rule: JsonDict) -> str:
    remediation = rule.get("remediation")
    if isinstance(remediation, str) and remediation.strip():
        return remediation.strip()
    return _DEFAULT_ACTION


def _finding(rule: JsonDict, facts: JsonDict) -> CommentaryFinding:
    meaning, why = _meaning_and_why(rule, facts)
    why = _with_covered_note(rule, why)
    label, urls = _location_bullets(rule, facts)
    return CommentaryFinding(
        severity=_rule_severity(rule),
        title=_finding_title(rule),
        meaning=meaning,
        why=why,
        explanation="",
        location_label=label,
        location_urls=urls,
        evidence_refs=_evidence_refs(rule),
        action_items=[_rule_action(rule)],
        tier=_tier(rule.get("tier")),
    )


def _recommendation(rule: JsonDict, facts: JsonDict) -> CommentaryRecommendation:
    action = _rule_action(rule)
    meaning, why = _meaning_and_why(rule, facts)
    why = _with_covered_note(rule, why)
    label, urls = _location_bullets(rule, facts)
    rationale = " ".join(part for part in (meaning, why) if part).strip()
    return CommentaryRecommendation(
        tier=_tier(rule.get("tier")),
        title=_recommendation_title(rule),
        rationale=rationale,
        action_items=[action],
        location_label=label,
        location_urls=urls,
    )


def _lead_section(scores: JsonDict) -> CommentarySection:
    lead = _int(scores.get("lead_gen"))
    seo = _int(scores.get("seo"))
    uxui = _int(scores.get("uxui"))
    severity = "high" if lead < 50 else "medium" if lead < 75 else "info"
    return CommentarySection(
        headline=f"Lead Generation Readiness score is {lead}",
        findings=[
            CommentaryFinding(
                severity=severity,
                title=f"Lead Generation Readiness score is {lead}",
                explanation=(
                    f"Lead Generation Readiness is the combined score from SEO and UX/UI. "
                    f"In this audit, SEO is {seo} and UX/UI is {uxui}, so the combined "
                    f"readiness score is {lead}. Improving the lower-scoring SEO and UX/UI "
                    "items in this report is what raises this number."
                ),
                evidence_refs=["scores.lead_gen"],
            )
        ],
        # The roadmap is driven by the concrete SEO/UX-UI recommendations; the composite
        # section adds no generic filler recommendations of its own.
        recommendations=[],
    )


def _opportunity_lead_in(opportunity: JsonDict) -> str:
    """P1: lead the executive summary with the business outcome, not the score, when Search Console
    data is available. Every number here is a stored GSC fact (grounding keeps it); ranges use
    "to" (never a hyphen) so the grounding validator does not read the upper bound as negative."""
    if not opportunity:
        return ""
    days = _int(opportunity.get("window_days"))
    site_clicks = _int(opportunity.get("site_monthly_clicks"))
    impressions = _int(opportunity.get("total_striking_impressions"))
    queries = _int(opportunity.get("striking_query_count"))
    modeled = _int(opportunity.get("modeled_query_count"))
    pos_min = _int(opportunity.get("striking_position_min"))
    pos_max = _int(opportunity.get("striking_position_max"))
    clicks_low = _int(opportunity.get("opportunity_clicks_low"))
    clicks_high = _int(opportunity.get("opportunity_clicks_high"))
    leads_low = _int(opportunity.get("estimated_leads_low"))
    leads_high = _int(opportunity.get("estimated_leads_high"))
    rate_low = _int(opportunity.get("lead_rate_low_pct"))
    rate_high = _int(opportunity.get("lead_rate_high_pct"))
    if queries <= 0 or impressions <= 0 or clicks_high <= 0:
        return ""
    # "it" needs the site_line antecedent; without one, name the subject explicitly.
    site_line = (
        f"the site currently earns about {site_clicks} Google clicks a month, and "
        if site_clicks > 0
        else ""
    )
    subject = "it" if site_line else "the site"
    # "0 to 0 extra inquiries" reads as a broken report — only quantify leads when the
    # conservative click range is large enough to yield at least one.
    leads_clause = (
        f", which is about {leads_low} to {leads_high} extra inquiries at a typical "
        f"{rate_low}% to {rate_high}% home-services contact rate"
        if leads_high > 0
        else ""
    )
    return (
        f"Based on your last {days} days of Search Console data, {site_line}searchers see "
        f"{subject} about {impressions} times a month for near-miss queries — searches where it "
        f"already ranks in positions {pos_min} to {pos_max}, just below the top results. A "
        f"conservative scenario that lifts the top {modeled} of those {queries} near-miss "
        f"queries toward the top of page one could add roughly {clicks_low} to {clicks_high} "
        f"visits a month{leads_clause}. This is a projection from your own Search "
        "Console data and published click-through benchmarks, not a promise, but it shows where "
        "the fastest wins are. "
    )


def _executive_summary(scores: JsonDict, top_label: str | None, opportunity: JsonDict) -> str:
    seo = _int(scores.get("seo"))
    uxui = _int(scores.get("uxui"))
    lead = _int(scores.get("lead_gen"))
    summary = _opportunity_lead_in(opportunity) + (
        f"This audit scored the site {seo} for SEO, {uxui} for UX/UI, and {lead} for "
        "Lead Generation Readiness. Lead Generation Readiness is the roll-up score that "
        "shows whether search visibility and the on-page conversion experience are working "
        "together."
    )
    if seo + 10 < uxui:
        summary += " The UX/UI experience is much stronger than the SEO foundation, so search visibility is the main reason the combined score is lower."
    elif uxui + 10 < seo:
        summary += " SEO is stronger than the on-page conversion experience, so UX/UI is the main reason the combined score is lower."
    if top_label:
        summary += f" The highest-priority opportunity is: {top_label}."
    summary += " Start with the issues that block visitors or search engines first, then move into the content and conversion improvements."
    return summary


def _top_priority_label(score_breakdown: JsonDict) -> str | None:
    # Merge before picking, so the label always names a card the findings section renders
    # (an unmerged secondary can outrank its primary yet never appear as its own card).
    surfaced = _merge_overlapping_rules(
        _surfaced_rules("seo", score_breakdown)
    ) + _merge_overlapping_rules(_surfaced_rules("uxui", score_breakdown))
    if not surfaced:
        return None
    top = min(surfaced, key=_finding_sort_key)
    return _finding_title(top)


# --- prose helpers (grounding-safe: numbers come only from evidence.value) ------------


def _finding_title(rule: JsonDict) -> str:
    label = rule.get("finding_label")
    if isinstance(label, str) and label.strip():
        return " ".join(label.split())
    description = rule.get("description")
    if isinstance(description, str) and description.strip():
        return " ".join(description.split())
    return "Improvement opportunity"


def _as_headline(text: str) -> str:
    """Collapse whitespace and drop a trailing period so an action sentence reads
    as a card headline."""
    return " ".join(text.split()).rstrip(".")


def _recommendation_title(rule: JsonDict) -> str:
    """Action-first headline for a recommendation card.

    A finding states the PROBLEM ("Pages do not use a single clear H1 heading");
    a recommendation must state the FIX ("Give every page one clear H1 heading")
    so the two never read as the same sentence. Hand-written titles live in
    ``_ACTION_TITLES``; a rule without one falls back to its remediation phrased
    as a headline, then to the finding label as a last resort - so a
    recommendation is never a verbatim restatement of its finding."""
    rule_id = str(rule.get("rule_id") or "")
    title = _ACTION_TITLES.get(rule_id)
    if title:
        return title
    remediation = rule.get("remediation")
    if isinstance(remediation, str) and remediation.strip():
        return _as_headline(remediation)
    return _finding_title(rule)


# Action-first recommendation headlines, keyed by rule_id. Each is the FIX as a
# short imperative, deliberately distinct from the rule's problem-stating
# ``finding_label`` so a report never shows the same sentence as both the problem
# and the fix. Keep these in sync with the rule set in rubrics/*.yaml.
_ACTION_TITLES: dict[str, str] = {
    # --- SEO ---
    "seo.title.present_all_pages": "Add a title tag to every page",
    # Headlines stay number-free on purpose: grounding strips unsupported numeric
    # claims from titles, and the exact target ranges already live in the
    # remediation ("DO THIS"), which is grounding-exempt.
    "seo.homepage_title.reasonable_length": "Adjust the homepage title length",
    "seo.meta_description.present_all_pages": "Add a meta description to every page",
    "seo.homepage_meta_description.reasonable_length": (
        "Adjust the homepage meta description length"
    ),
    "seo.h1.present_once": "Give every page one clear H1 heading",
    "seo.homepage.canonical": "Add a canonical tag to the homepage",
    "seo.schema.present": "Add structured-data markup to key pages",
    "seo.schema.business_identity": "Add business-identity structured data",
    "seo.schema.valid_json_ld": "Fix malformed structured data",
    "seo.schema.breadcrumb": "Add breadcrumb structured data",
    "seo.images.alt_coverage": "Raise alt-text coverage across images",
    "seo.indexability.no_noindex_pages": "Unblock pages from search indexing",
    "seo.security.https": "Serve the whole site over HTTPS",
    "seo.security.no_mixed_content": "Fix insecure mixed-content resources",
    "seo.aeo.heading_hierarchy": "Clean up the heading outline",
    "seo.aeo.question_headings": "Phrase key subheadings as questions",
    "seo.aeo.extractable_structure": "Use scannable lists and tables",
    "seo.local.nap_schema": "Complete the business NAP structured data",
    "seo.local.service_area": "Declare your service area in structured data",
    "seo.local.map_or_gbp": "Link to your Google Business Profile",
    "seo.local.visible_address": "Show your business address on the page",
    "seo.a11y.html_lang": "Declare the page language",
    "seo.a11y.viewport_zoom": "Let visitors zoom the page",
    "seo.a11y.main_landmark": "Add a main content landmark",
    "seo.a11y.no_positive_tabindex": "Fix the keyboard tab order",
    "seo.a11y.form_controls_labeled": "Label every form field",
    "seo.a11y.links_have_name": "Give every link readable text",
    "seo.a11y.buttons_have_name": "Give every button a readable label",
    "seo.a11y.unique_referenced_ids": "Make referenced element IDs unique",
    "seo.internal_links.depth": "Strengthen internal links between pages",
    "seo.psi.mobile_performance": "Speed up page loads on mobile",
    "seo.psi.desktop_performance": "Speed up page loads on desktop",
    "seo.cwv.lcp": "Improve real-user loading speed (LCP)",
    "seo.cwv.inp": "Improve interaction responsiveness (INP)",
    "seo.cwv.cls": "Stop the page from shifting as it loads (CLS)",
    "seo.technical_crawl.no_broken_internal_urls": "Repair broken links and error pages",
    "seo.technical_crawl.indexable_urls": "Let blocked pages show in search",
    "seo.technical_crawl.canonicals": "Add canonical tags to pages",
    "seo.technical_crawl.redirect_chains": "Remove internal redirect chains",
    "seo.technical_crawl.missing_titles": "Add title tags to untitled pages",
    "seo.technical_crawl.duplicate_titles": "Make every page title unique",
    "seo.technical_crawl.missing_meta_descriptions": "Add the missing meta descriptions",
    "seo.technical_crawl.missing_h1": "Add an H1 to pages that lack one",
    "seo.technical_crawl.missing_image_alt": "Add alt text to images that lack it",
    "seo.gsc.low_ctr_pages": "Turn high-impression pages into clicks",
    "seo.gsc.ranking_opportunities": "Push near-page-one keywords onto page one",
    "seo.gsc.url_inspection_indexing": "Get priority pages indexed by Google",
    # --- UX/UI ---
    "uxui.primary_cta.present": "Add a clear primary call to action",
    "uxui.cta.volume": "Add more conversion paths across the site",
    "uxui.cta.above_fold": "Put a call to action above the fold",
    "uxui.forms.present": "Add a short lead-capture form",
    "uxui.homepage_form.field_count": "Right-size the homepage lead form",
    "uxui.phone.visible": "Show a clickable phone number",
    "uxui.contact_path.low_pressure": "Give visitors an easy low-pressure contact path",
    "uxui.trust.present": "Add trust signals like reviews and testimonials",
    "uxui.trust.depth": "Add more types of trust evidence",
    "uxui.navigation.present": "Add clear primary navigation",
    "uxui.copy.substantial": "Expand the copy to explain the offer",
    "uxui.direct_contact.present": "Give visitors a direct way to make contact",
    "uxui.lead_capture.cta": "Add a call to action to the homepage",
}


def _meaning_and_why(rule: JsonDict, facts: JsonDict) -> tuple[str, str]:
    """Return ("what it means" + measurement, "why it matters") as two card-ready
    strings. Numbers come only from the rule's stored ``evidence.value`` or other stored
    facts, so grounding keeps them; no URLs are included (those go in the location list)."""
    rule_id = str(rule.get("rule_id") or "")
    context = _RULE_CONTEXT.get(rule_id, _GENERIC_CONTEXT)
    meaning_parts = [context.get("meaning"), _evidence_sentence(rule, context, facts)]
    meaning = " ".join(part for part in meaning_parts if part).strip()
    why = str(context.get("why") or "").strip()
    return meaning, why


def _range_finding_sentence(context: JsonDict, facts: JsonDict) -> str | None:
    """For a boolean "is in the ideal range" check, state the measured value AND the
    baseline range it was judged against. Both come from stored facts (the extractor
    exposes ``length`` plus ``ideal_min_length``/``ideal_max_length``), so grounding keeps
    every number."""
    spec = _dict(context.get("range_finding"))
    if not spec:
        return None
    length = resolve_fact_path(facts, str(spec.get("length_path") or ""))
    low = resolve_fact_path(facts, str(spec.get("min_path") or ""))
    high = resolve_fact_path(facts, str(spec.get("max_path") or ""))
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in (length, low, high)
    ):
        return None
    subject = str(spec.get("subject") or "value")
    unit = str(spec.get("unit") or "characters")
    if length < low:
        relation = "shorter than"
    elif length > high:
        relation = "longer than"
    else:
        relation = "outside"
    return (
        f"The {subject} is {length} {unit}, {relation} the useful range of {low} to {high} {unit}."
    )


def _evidence_sentence(rule: JsonDict, context: JsonDict, facts: JsonDict) -> str | None:
    value = _dict(rule.get("evidence")).get("value")
    number = _format_number(value)
    if number is None:
        if value is False:
            return _range_finding_sentence(context, facts) or str(context.get("failed_check") or "")
        return None
    fact_path = str(rule.get("fact_path") or "")
    if fact_path.endswith("_pct"):
        return f"The audit measured this at {number}% across the analyzed pages."
    noun = str(context.get("noun") or "item")
    # Multi-word noun phrases pluralize their head word, not the phrase end
    # ("pages missing a title tag", never "page missing a title tags").
    plural = str(context.get("noun_plural") or f"{noun}s")
    return f"The audit found {number} {noun if number == '1' else plural}."


def _evidence_refs(rule: JsonDict) -> list[str]:
    fact_path = rule.get("fact_path")
    return [str(fact_path)] if isinstance(fact_path, str) and fact_path else []


_GENERIC_CONTEXT = {
    "meaning": "This finding marks a check that did not fully pass.",
    "why": "It matters because the issue can make the page harder to understand, trust, or act on.",
    "noun": "item",
    "failed_check": "The audit check did not pass for the analyzed page or pages.",
}

_RULE_CONTEXT: dict[str, JsonDict] = {
    "seo.title.present_all_pages": {
        "meaning": "A title tag is the page name Google and searchers use to understand a result.",
        "why": "Missing titles make pages harder to identify in search and can lower clicks from people who would otherwise be interested.",
        "noun": "page missing a title tag",
        "noun_plural": "pages missing a title tag",
    },
    "seo.homepage_title.reasonable_length": {
        "meaning": "The homepage title is the main search-result headline for the most important page on the site.",
        "why": "If it is too short, too long, or unclear, Google may rewrite it and searchers may not understand the offer quickly.",
        "failed_check": "The homepage title did not fall inside the useful search-result length range.",
        "range_finding": {
            "subject": "homepage title",
            "unit": "characters",
            "length_path": "seo.pages[0].title.length",
            "min_path": "seo.pages[0].title.ideal_min_length",
            "max_path": "seo.pages[0].title.ideal_max_length",
        },
    },
    "seo.meta_description.present_all_pages": {
        "meaning": "A meta description is the short search-result summary that helps someone decide whether to click.",
        "why": "Missing descriptions leave Google to generate its own snippet, which may be less persuasive than a message written for buyers.",
        "noun": "page missing a meta description",
        "noun_plural": "pages missing a meta description",
    },
    "seo.homepage_meta_description.reasonable_length": {
        "meaning": "The homepage meta description should summarize the offer in a short, useful search-result snippet.",
        "why": "When the description is outside the useful range, the result can look weak, truncated, or auto-written by Google.",
        "failed_check": "The homepage meta description did not fall inside the useful search-result length range.",
        "range_finding": {
            "subject": "homepage meta description",
            "unit": "characters",
            "length_path": "seo.pages[0].meta_description.length",
            "min_path": "seo.pages[0].meta_description.ideal_min_length",
            "max_path": "seo.pages[0].meta_description.ideal_max_length",
        },
    },
    "seo.h1.present_once": {
        "meaning": "An H1 is the main visible heading that tells visitors and search engines what a page is about.",
        "why": "Pages with no H1 or multiple competing H1s can feel less clear and send weaker topic signals.",
        "noun": "page with one clear H1",
        "noun_plural": "pages with one clear H1",
    },
    "seo.homepage.canonical": {
        "meaning": "A canonical URL tells Google which version of a page should be treated as the preferred version.",
        "why": "Without it, duplicate URLs can split ranking signals or cause Google to choose a less useful version.",
        "failed_check": "The homepage canonical URL was not detected in the analyzed HTML.",
    },
    "seo.schema.present": {
        "meaning": "Schema markup is structured data that gives search engines extra context about the business, service, or page.",
        "why": "Without it, Google has fewer explicit clues for understanding the business and showing richer search results.",
        "noun": "page with schema markup",
        "noun_plural": "pages with schema markup",
    },
    "seo.images.alt_coverage": {
        "meaning": "Alt text describes meaningful images for screen readers and gives search engines context about the page.",
        "why": "Low coverage weakens accessibility and can make image-heavy sections less understandable to search engines.",
        "noun": "percent image alt-text coverage",
    },
    "seo.indexability.no_noindex_pages": {
        "meaning": "A noindex directive tells search engines not to include a page in search results.",
        "why": "If a lead page is accidentally noindexed, it can be invisible in Google even when the content is useful.",
        "noun": "noindex page",
    },
    "seo.internal_links.depth": {
        "meaning": "Internal links help visitors and search engines move from one useful page to the next.",
        "why": "Thin internal linking can leave good pages isolated and make next steps harder to find.",
        "noun": "internal link",
    },
    "seo.aeo.heading_hierarchy": {
        "meaning": "A clean heading outline uses one H1 and steps down through H2/H3 without skipping levels.",
        "why": "A broken outline makes the page harder for readers, screen readers, and answer engines to segment into the right sections.",
        "failed_check": "The heading outline either repeated the H1 or jumped past a heading level on an analyzed page.",
    },
    "seo.aeo.question_headings": {
        "meaning": "Question-style subheadings phrase a section around the exact question a buyer would ask.",
        "why": "They line up with how people and AI assistants search, so the page is easier to match to a query and quote in an answer.",
        "noun": "question-style subheading",
        "noun_plural": "question-style subheadings",
    },
    "seo.aeo.extractable_structure": {
        "meaning": "Lists and tables turn steps, services, and specs into scannable chunks instead of dense paragraphs.",
        "why": "Scannable structure helps visitors skim and lets answer engines lift a clean, self-contained block from the page.",
        "failed_check": "No genuine content list or comparison table was found in the main content of the analyzed page or pages.",
    },
    "seo.local.nap_schema": {
        "meaning": "NAP structured data spells out the business name, postal address, and phone in a machine-readable LocalBusiness record.",
        "why": "Without complete NAP markup, search engines and AI assistants cannot confidently tie the site to a real, located business.",
        "failed_check": "No LocalBusiness structured data with a complete name, address, and phone was found on the analyzed page or pages.",
    },
    "seo.local.service_area": {
        "meaning": "A declared service area (areaServed or geo) tells search engines which places the business serves.",
        "why": "Local searches happen in specific cities, so an undeclared service area can keep the business out of the right local results.",
        "failed_check": "No service area or geo coordinates were declared in the structured data on the analyzed page or pages.",
    },
    "seo.local.map_or_gbp": {
        "meaning": "A Google Business Profile or map link connects the website to the verified, reviewed local listing.",
        "why": "That link reinforces the business as a real local entity and gives visitors a fast way to check the location and reviews.",
        "failed_check": "No Google Business Profile or map link was found on the analyzed page or pages.",
    },
    "seo.local.visible_address": {
        "meaning": "A visible address block shows the business location to visitors, mirroring the structured-data NAP.",
        "why": "A visible, consistent address builds trust and confirms to search engines that the structured-data location is genuine.",
        "failed_check": "No visible address block was found on the analyzed page or pages.",
    },
    "seo.a11y.html_lang": {
        "meaning": "The html element's lang attribute tells assistive technology which language the page is written in.",
        "why": "Without it, screen readers can mispronounce the content, making the page harder to use for visitors who rely on them.",
        "failed_check": "An analyzed page did not declare a language on its html element.",
    },
    "seo.a11y.viewport_zoom": {
        "meaning": "The viewport meta tag can either allow or block a visitor from pinching to zoom in.",
        "why": "Blocking zoom stops low-vision visitors from enlarging text, which is both an accessibility barrier and a conversion risk.",
        "failed_check": "An analyzed page's viewport tag disables zooming (user-scalable=no or a low maximum-scale).",
    },
    "seo.a11y.main_landmark": {
        "meaning": "A main landmark marks the primary content region so assistive tech can jump past the header and navigation.",
        "why": "Without it, screen-reader and keyboard users must wade through the menus on every page to reach the content.",
        "failed_check": 'An analyzed page has no main element or role="main" landmark.',
    },
    "seo.a11y.no_positive_tabindex": {
        "meaning": "A positive tabindex forces an element earlier in the keyboard tab order than its position on the page.",
        "why": "It desyncs the visual order from the focus order, so keyboard users can jump around unpredictably.",
        "noun": "element with a positive tabindex",
        "noun_plural": "elements with a positive tabindex",
    },
    "seo.a11y.form_controls_labeled": {
        "meaning": "A programmatic label ties a visible name to a form field so screen readers announce what it is for.",
        "why": "Unlabeled fields are one of the most common accessibility failures and leave some visitors unable to complete the form.",
        "noun": "form field with no programmatic label",
        "noun_plural": "form fields with no programmatic label",
    },
    "seo.a11y.links_have_name": {
        "meaning": "Every link needs readable text or an accessible label — especially icon-only links with no visible words.",
        "why": 'A link announced as just "link" gives screen-reader users no idea where it goes, so they skip it.',
        "noun": "link with no readable text",
        "noun_plural": "links with no readable text",
    },
    "seo.a11y.buttons_have_name": {
        "meaning": "Every button needs readable text or an accessible label so its action is announced.",
        "why": 'An unlabeled button is announced as just "button," leaving assistive-tech users unsure what it does.',
        "noun": "button with no readable label",
        "noun_plural": "buttons with no readable label",
    },
    "seo.a11y.unique_referenced_ids": {
        "meaning": "When a label or ARIA attribute points at an element ID, that ID must be unique to resolve to the right control.",
        "why": "A duplicated, referenced ID silently links the label to the wrong element, breaking the association for assistive tech.",
        "noun": "duplicated referenced ID",
        "noun_plural": "duplicated referenced IDs",
    },
    "seo.psi.mobile_performance": {
        "meaning": "Mobile PageSpeed reflects how quickly and smoothly the site loads for people on phones.",
        "why": "Slow mobile pages can frustrate visitors before they read the offer and can weaken search performance.",
        "noun": "mobile performance point",
    },
    "seo.psi.desktop_performance": {
        "meaning": "Desktop PageSpeed reflects how quickly and smoothly the site loads on larger screens.",
        "why": "Even when the design is strong, slow loading can reduce trust and make forms or calls to action feel harder to reach.",
        "noun": "desktop performance point",
    },
    "seo.technical_crawl.no_broken_internal_urls": {
        "meaning": "A broken internal URL is a link found during the site health check that returned an error instead of a usable page.",
        "why": "Visitors and search engines can hit a dead end, which hurts trust, search visibility, and the path to conversion.",
        "noun": "broken internal URL",
        "noun_plural": "broken internal URLs",
    },
    "seo.technical_crawl.indexable_urls": {
        "meaning": "A non-indexable internal URL is a page the site health check found but Google may not be able to include in search results.",
        "why": "If these are important service, blog, or landing pages, they may not bring organic traffic.",
        "noun": "non-indexable internal URL",
        "noun_plural": "non-indexable internal URLs",
    },
    "seo.technical_crawl.missing_titles": {
        "meaning": "A missing title tag means the page does not provide a clear search-result headline.",
        "why": "That makes the page harder for Google and buyers to understand before they click.",
        "noun": "page missing a title tag",
        "noun_plural": "pages missing a title tag",
    },
    "seo.technical_crawl.duplicate_titles": {
        "meaning": "Duplicate title tags mean multiple pages are using the same search-result headline.",
        "why": "Google may treat those pages as interchangeable and the wrong page can rank or receive clicks.",
        "noun": "page with a duplicate title tag",
        "noun_plural": "pages with a duplicate title tag",
    },
    "seo.technical_crawl.missing_meta_descriptions": {
        "meaning": "A missing meta description means the page does not provide a written search-result summary.",
        "why": "Google may invent a less persuasive snippet, which can reduce clicks from searchers.",
        "noun": "page missing a meta description",
        "noun_plural": "pages missing a meta description",
    },
    "seo.technical_crawl.missing_h1": {
        "meaning": "A missing H1 means the page lacks a clear main visible heading.",
        "why": "Visitors may need more effort to understand the page, and search engines get a weaker topic signal.",
        "noun": "page missing an H1 heading",
        "noun_plural": "pages missing an H1 heading",
    },
    "seo.technical_crawl.missing_image_alt": {
        "meaning": "Missing image alt text means meaningful images do not have a text description.",
        "why": "That weakens accessibility and removes context that can help search engines understand the page.",
        "noun": "image missing alt text",
        "noun_plural": "images missing alt text",
    },
    "seo.gsc.low_ctr_pages": {
        "meaning": "Search Console shows pages that appear often in Google but are not earning enough clicks.",
        "why": "Those pages already have visibility, so better titles, descriptions, and offer framing can turn existing impressions into more visits.",
        "noun": "high-impression low-click page",
        "noun_plural": "high-impression low-click pages",
    },
    "seo.gsc.ranking_opportunities": {
        "meaning": "Search Console shows queries where the site is close enough to compete but not yet in the strongest positions.",
        "why": "Improving the matching page can lift traffic without starting from zero because Google already associates the site with the topic.",
        "noun": "ranking opportunity",
        "noun_plural": "ranking opportunities",
    },
    "seo.gsc.url_inspection_indexing": {
        "meaning": "URL Inspection checks whether priority pages are actually available in Google.",
        "why": "If an important page is not indexed, content and design improvements will not help that page earn search traffic.",
        "noun": "priority URL not on Google",
        "noun_plural": "priority URLs not on Google",
    },
    "uxui.primary_cta.present": {
        "meaning": "A primary call to action is the main next step you want a visitor to take.",
        "why": "Without a clear primary action, interested visitors can hesitate or leave instead of contacting the business.",
        "noun": "page with a primary call to action",
        "noun_plural": "pages with a primary call to action",
    },
    "uxui.cta.volume": {
        "meaning": "Conversion paths are the buttons, links, and contact prompts that move a visitor toward becoming a lead.",
        "why": "Too few paths can make the site feel informational instead of action-oriented.",
        "noun": "call to action",
        "noun_plural": "calls to action",
    },
    "uxui.cta.above_fold": {
        "meaning": "Above-the-fold calls to action are visible early, before a visitor scrolls.",
        "why": "If the first screen does not show a next step, motivated buyers may miss the fastest way to contact the business.",
        "noun": "early call to action",
        "noun_plural": "early calls to action",
    },
    "uxui.forms.present": {
        "meaning": "A lead capture form - on the page or as an embedded/popup form - gives visitors a direct way to request contact or start a conversation.",
        "why": "Without any form, some visitors will not take the next step even if the offer is relevant.",
        "noun": "page with a lead form",
        "noun_plural": "pages with a lead form",
    },
    "uxui.homepage_form.field_count": {
        "meaning": "The homepage form should be short enough that a serious buyer can complete it without friction.",
        "why": "If the homepage has no practical form, or the form asks for too much, lead capture depends too heavily on other contact paths.",
        "noun": "homepage form field",
    },
    "uxui.phone.visible": {
        "meaning": "A visible phone number gives ready-to-talk visitors a direct contact path.",
        "why": "If phone contact is hard to find, high-intent visitors may delay or choose a competitor.",
        "noun": "page with a visible phone number",
        "noun_plural": "pages with a visible phone number",
    },
    "uxui.contact_path.low_pressure": {
        "meaning": "A low-pressure contact path is an easy first step for visitors who are not ready to call - a visible email, a contact page link, or a short lead form.",
        "why": "Without one, phone-shy prospects have no comfortable way to start the conversation, which reduces inquiries.",
        "noun": "page with a low-pressure contact path",
        "noun_plural": "pages with a low-pressure contact path",
    },
    "uxui.trust.present": {
        "meaning": "Trust signals are proof points such as reviews, testimonials, certifications, awards, or credible case studies.",
        "why": "Without proof, visitors must take the offer on faith, which makes lead conversion harder.",
        "noun": "page with trust evidence",
        "noun_plural": "pages with trust evidence",
    },
    "uxui.trust.depth": {
        "meaning": "Trust depth means the site uses more than one type of proof.",
        "why": "A mix of proof points is more convincing than one isolated badge or testimonial.",
        "noun": "trust signal",
    },
    "uxui.navigation.present": {
        "meaning": "Primary navigation helps visitors find key pages such as services, proof, pricing context, or contact.",
        "why": "Missing or unclear navigation creates friction before a visitor reaches the information they need.",
        "failed_check": "Primary navigation was not detected on the homepage.",
    },
    "uxui.copy.substantial": {
        "meaning": "Substantial page copy explains the offer, who it is for, and what happens next.",
        "why": "Thin copy can leave visitors with unanswered questions and reduce confidence before they contact the business.",
        "failed_check": "The homepage copy was thinner than the audit expects for a lead-generation page.",
    },
    "uxui.direct_contact.present": {
        "meaning": "Direct contact means a visitor can reach the business without hunting for another page.",
        "why": "When contact details are not obvious, high-intent visitors can drop off before converting.",
        "failed_check": "The homepage did not show a direct contact path in the analyzed HTML.",
    },
    "uxui.lead_capture.cta": {
        "meaning": "A homepage call to action tells visitors exactly what to do next.",
        "why": "Without it, the page can explain the business but still fail to create leads.",
        "failed_check": "The homepage did not show a clear call to action in the analyzed HTML.",
    },
}


def _location_bullets(rule: JsonDict, facts: JsonDict) -> tuple[str, list[str]]:
    """Return (label, bullet items) for "where to start", so the report can render the
    locations as a list instead of cramming URLs into the end of a paragraph."""
    rule_id = str(rule.get("rule_id") or "")
    examples = _location_examples(rule_id, facts)
    if examples:
        return "Start by checking", [str(example) for example in examples[:3]]

    if "pages[0]" in str(rule.get("fact_path") or ""):
        homepage = _first_page_url(facts)
        if homepage:
            return "Start by checking the homepage", [homepage]
    return "", []


def _location_examples(rule_id: str, facts: JsonDict) -> list[str]:
    if rule_id.startswith("seo.technical_crawl."):
        issue_id = _TECHNICAL_CRAWL_RULE_TO_ISSUE.get(rule_id)
        external = _dict(facts.get("external_seo"))
        technical = _dict(external.get("technical_crawl") or external.get("screaming_frog"))
        issues = _list(technical.get("issues"))
        for issue in issues:
            payload = _dict(issue)
            if payload.get("id") == issue_id:
                return [str(value) for value in _list(payload.get("examples")) if value]

    if rule_id == "seo.gsc.low_ctr_pages":
        rows = _list(
            _dict(_dict(facts.get("external_seo")).get("gsc")).get("high_impression_low_ctr_pages")
        )
        return [str(_dict(row).get("page")) for row in rows if _dict(row).get("page")]

    if rule_id == "seo.gsc.ranking_opportunities":
        rows = _list(
            _dict(_dict(facts.get("external_seo")).get("gsc")).get("ranking_opportunities")
        )
        return [f"the query '{_dict(row).get('query')}'" for row in rows if _dict(row).get("query")]

    if rule_id == "seo.gsc.url_inspection_indexing":
        rows = _list(_dict(_dict(facts.get("external_seo")).get("url_inspection")).get("items"))
        return [
            str(_dict(row).get("url"))
            for row in rows
            if _dict(row).get("on_google") is False and _dict(row).get("url")
        ]

    if rule_id in {"seo.title.present_all_pages", "seo.meta_description.present_all_pages"}:
        key = "title" if rule_id == "seo.title.present_all_pages" else "meta_description"
        return [
            str(page.get("url"))
            for page in _seo_pages(facts)
            if not _dict(page.get(key)).get("present") and page.get("url")
        ]

    if rule_id == "seo.h1.present_once":
        return [
            str(page.get("url"))
            for page in _seo_pages(facts)
            if _dict(_dict(page.get("headings")).get("counts")).get("h1") != 1 and page.get("url")
        ]

    if rule_id == "seo.images.alt_coverage":
        return [
            str(page.get("url"))
            for page in _seo_pages(facts)
            if int(_dict(page.get("images")).get("missing_alt") or 0) > 0 and page.get("url")
        ]

    if rule_id == "seo.indexability.no_noindex_pages":
        return [
            str(page.get("url"))
            for page in _seo_pages(facts)
            if _dict(page.get("robots")).get("noindex") and page.get("url")
        ]

    if rule_id.startswith("seo.psi."):
        rows = _list(_dict(_dict(facts.get("psi")).get("summary")).get("slowest_pages"))
        return [str(_dict(row).get("url")) for row in rows if _dict(row).get("url")]

    if rule_id == "uxui.homepage_form.field_count":
        homepage = _first_page_url(facts, source="uxui")
        return [f"the homepage form area ({homepage})"] if homepage else ["the homepage form area"]

    if rule_id.startswith("uxui."):
        return _uxui_relevant_pages(rule_id, facts)

    return []


_TECHNICAL_CRAWL_RULE_TO_ISSUE = {
    "seo.technical_crawl.no_broken_internal_urls": "client_error_internal_urls",
    "seo.technical_crawl.indexable_urls": "non_indexable_internal_urls",
    "seo.technical_crawl.missing_titles": "missing_titles",
    "seo.technical_crawl.duplicate_titles": "duplicate_titles",
    "seo.technical_crawl.missing_meta_descriptions": "missing_meta_descriptions",
    "seo.technical_crawl.missing_h1": "missing_h1",
    "seo.technical_crawl.missing_image_alt": "images_missing_alt",
}


def _seo_pages(facts: JsonDict) -> list[JsonDict]:
    return [_dict(page) for page in _list(_dict(facts.get("seo")).get("pages"))]


def _uxui_pages(facts: JsonDict) -> list[JsonDict]:
    return [_dict(page) for page in _list(_dict(facts.get("uxui")).get("pages"))]


def _first_page_url(facts: JsonDict, *, source: str = "seo") -> str | None:
    pages = _seo_pages(facts) if source == "seo" else _uxui_pages(facts)
    return str(pages[0].get("url")) if pages and pages[0].get("url") else None


def _uxui_relevant_pages(rule_id: str, facts: JsonDict) -> list[str]:
    pages = _uxui_pages(facts)
    if rule_id in {"uxui.primary_cta.present", "uxui.lead_capture.cta", "uxui.cta.volume"}:
        return [
            str(page.get("url"))
            for page in pages
            if not _dict(page.get("lead_capture")).get("has_cta") and page.get("url")
        ] or ([str(pages[0].get("url"))] if pages and pages[0].get("url") else [])
    if rule_id == "uxui.cta.above_fold":
        return [
            str(page.get("url"))
            for page in pages
            if int(_dict(page.get("ctas")).get("above_fold_count") or 0) == 0 and page.get("url")
        ]
    if rule_id in {"uxui.forms.present", "uxui.homepage_form.field_count"}:
        return [
            str(page.get("url"))
            for page in pages
            if str(_dict(page.get("forms")).get("form_detected") or "none") == "none"
            and page.get("url")
        ] or ([str(pages[0].get("url"))] if pages and pages[0].get("url") else [])
    if rule_id in {"uxui.phone.visible", "uxui.direct_contact.present"}:
        return [
            str(page.get("url"))
            for page in pages
            if not _dict(page.get("contact")).get("has_phone") and page.get("url")
        ]
    if rule_id == "uxui.contact_path.low_pressure":
        return [
            str(page.get("url"))
            for page in pages
            if not _dict(page.get("lead_capture")).get("has_low_pressure_path") and page.get("url")
        ]
    if rule_id in {"uxui.trust.present", "uxui.trust.depth"}:
        return [
            str(page.get("url"))
            for page in pages
            if not _dict(page.get("trust_signals")).get("has_trust_signals") and page.get("url")
        ]
    return [str(pages[0].get("url"))] if pages and pages[0].get("url") else []


def _format_number(value: Any) -> str | None:
    """Render a stored numeric fact for prose, preserving its exact value.

    Returns ``None`` for booleans/non-numbers so boolean facts (present/absent) are not
    rendered as numbers. Integral floats render without a decimal point; the parsed value
    still matches the stored fact, so grounding does not strip the sentence.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return None


def _severity(impact: Any, result: Any) -> Severity:
    impact_key = impact if impact in {"high", "medium", "low"} else "medium"
    result_key = result if result in {"fail", "partial"} else "fail"
    return _SEVERITY_MATRIX[(impact_key, result_key)]


def _tier(value: Any) -> Tier:
    return value if value in TIER_ORDER else "quick_win"


def _dict(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
