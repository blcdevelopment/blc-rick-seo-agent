from types import SimpleNamespace
from uuid import uuid4

from apps.worker.stages.report_payload import compose_report_payload


def test_compose_report_payload_includes_epic_4_contract() -> None:
    payload = compose_report_payload(_job(), _result())

    assert payload.version == "phase1-report-v3"
    assert payload.metadata.site_domain == "example.com"
    assert [score.id for score in payload.scores] == ["lead_gen", "seo", "uxui"]
    score_descriptions = {score.id: score.description for score in payload.scores}
    assert (
        "Formula: round((SEO 76 * 45%) + (UX/UI 68 * 55%)) = 72/100"
        in (score_descriptions["lead_gen"])
    )
    assert (
        "It evaluated 2 checks and earned 76 of 100 available points" in (score_descriptions["seo"])
    )
    assert "1 check worth 6 points was skipped" in score_descriptions["seo"]
    # Earned points already read as the /100 score, so the redundant
    # "normalized to" clause is suppressed for this section.
    assert "earned 68 of 100 available points." in score_descriptions["uxui"]
    assert "normalized to" not in score_descriptions["uxui"]
    assert payload.sections[0].id == "seo"
    assert payload.sections[1].id == "uxui"
    assert payload.roadmap[0].tier == "quick_win"
    assert payload.validation_summary.unsupported_claim_count == 1
    assert payload.pagespeed_summary.status == "skipped"
    assert payload.pagespeed_summary.reason == (
        "No Google PageSpeed API key is configured, so page speed was not measured."
    )
    assert payload.external_seo_summary.status == "partial"
    assert payload.technical_seo_section.issues[0].id == "client_error_internal_urls"
    assert payload.technical_seo_section.issues[0].summary.startswith("These URLs on the site")
    assert "Visitors" in payload.technical_seo_section.issues[0].why_it_matters
    assert "Update the link" in payload.technical_seo_section.issues[0].recommended_fix
    assert payload.technical_seo_section.issues[0].location_label == (
        "Affected URL found during the check"
    )
    assert payload.search_performance_section.ranking_opportunities[0]["query"] == "custom homes"
    assert payload.crawl_summary.failed_pages == 1
    assert payload.appendix.seo_rules
    assert payload.appendix.uxui_rules


def test_compose_report_payload_falls_back_to_rubric_findings_without_commentary() -> None:
    result = _result(commentary={"status": "complete", "content": {}})

    payload = compose_report_payload(_job(), result)

    seo = next(section for section in payload.sections if section.id == "seo")
    assert seo.findings[0].source == "rubric"
    assert seo.findings[0].evidence_refs == ["seo.meta_description.present_all_pages"]


def test_compose_report_payload_does_not_surface_incomplete_external_zeroes() -> None:
    result = _result(
        external_seo_facts={
            "status": "partial",
            "sources": {
                "screaming_frog": "failed",
                "gsc": "skipped",
                "url_inspection": "failed",
            },
            "screaming_frog": {
                "status": "failed",
                "summary": {
                    "urls_crawled": 0,
                    "non_indexable_internal_urls": 0,
                },
                "issues": [
                    {
                        "id": "client_error_internal_urls",
                        "severity": "high",
                        "title": "Internal URLs returning 4xx errors",
                        "count": 0,
                        "examples": [],
                    }
                ],
            },
            "gsc": {
                "status": "skipped",
                "summary": {"top_query_count": 0, "top_page_count": 0},
                "ranking_opportunities": [{"query": "stale row"}],
                "high_impression_low_ctr_pages": [{"page": "https://example.com/"}],
                "declining_pages": [{"page": "https://example.com/old"}],
            },
            "url_inspection": {
                "status": "failed",
                "summary": {"not_on_google": 0},
                "items": [{"url": "https://example.com/"}],
            },
        }
    )

    payload = compose_report_payload(_job(), result)

    assert payload.external_seo_summary.technical_issue_count == 0
    assert payload.external_seo_summary.search_opportunity_count == 0
    assert payload.technical_seo_section.status == "failed"
    assert payload.technical_seo_section.summary == {}
    assert payload.technical_seo_section.issues == []
    assert payload.search_performance_section.status == "skipped"
    assert payload.search_performance_section.summary == {}
    assert payload.search_performance_section.ranking_opportunities == []
    assert payload.search_performance_section.url_inspection_items == []


def test_search_performance_section_carries_opportunity_branded_clusters() -> None:
    result = _result(
        external_seo_facts={
            "status": "complete",
            "sources": {"gsc": "complete"},
            "gsc": {
                "status": "complete",
                "site_url": "https://example.com/",
                "summary": {"top_query_count": 5},
                "top_queries": [],
                "top_pages": [],
                "ranking_opportunities": [],
                "high_impression_low_ctr_pages": [],
                "declining_pages": [],
                "opportunity": {
                    "is_estimate": True,
                    "opportunity_clicks_low": 120,
                    "opportunity_clicks_high": 310,
                    "striking_query_count": 4,
                },
                "branded": {"brand_token": "example", "branded_impression_share_pct": 35},
                "topic_clusters": [
                    {
                        "cluster": "remodel",
                        "query_count": 6,
                        "impressions": 900,
                        "avg_position": 7.2,
                    }
                ],
            },
        }
    )
    section = compose_report_payload(_job(), result).search_performance_section
    assert section.opportunity["opportunity_clicks_high"] == 310
    assert section.branded["branded_impression_share_pct"] == 35
    assert section.topic_clusters[0]["cluster"] == "remodel"


def test_search_performance_opportunity_empty_when_gsc_absent() -> None:
    section = compose_report_payload(_job(), _result()).search_performance_section
    assert section.opportunity == {}
    assert section.branded == {}
    assert section.topic_clusters == []


def test_accessibility_advisory_section_defaults_to_skipped() -> None:
    payload = compose_report_payload(_job(), _result())

    assert payload.accessibility_advisory_section.status == "skipped"
    assert payload.accessibility_advisory_section.issues == []
    assert payload.sections[0].score == 76  # scores untouched


def test_accessibility_advisory_section_renders_when_complete_without_changing_scores() -> None:
    facts = {
        "status": "complete",
        "axe_version": "4.10.2",
        "pages_scanned": 3,
        "impact_counts": {"critical": 1, "serious": 2, "moderate": 0, "minor": 0},
        "needs_review_count": 4,
        "disclaimer": "Advisory only — not a compliance verdict.",
        "notes": ["Some checks are scored above."],
        "issues": [
            {
                "rule_id": "color-contrast",
                "impact": "serious",
                "wcag_criteria": ["wcag143", "wcag2aa"],
                "help": "Elements must meet contrast thresholds",
                "help_url": "https://example.org/contrast",
                "instances": 12,
                "example_selectors": [".hero", ".footer a"],
                "example_pages": ["https://example.com/"],
                "failure_summary": "Fix the contrast ratio.",
            }
        ],
    }
    before = compose_report_payload(_job(), _result())
    after = compose_report_payload(_job(), _result(accessibility_facts=facts))

    section = after.accessibility_advisory_section
    assert section.status == "complete"
    assert section.axe_version == "4.10.2"
    assert section.pages_scanned == 3
    assert section.impact_counts == {"critical": 1, "serious": 2, "moderate": 0, "minor": 0}
    assert section.needs_review_count == 4
    assert section.disclaimer.startswith("Advisory only")
    assert section.issues[0].rule_id == "color-contrast"
    assert section.issues[0].wcag_criteria == ["wcag143", "wcag2aa"]
    # The advisory section NEVER changes scores.
    assert [card.score for card in after.scores] == [card.score for card in before.scores]
    assert [section.score for section in after.sections] == [
        section.score for section in before.sections
    ]


def test_social_section_absent_when_social_facts_empty_even_with_overall() -> None:
    # The two combined-audit extras are independent: an overall_readiness key in the breakdown
    # must not drag in a degenerate social section built from empty facts.
    breakdown = _score_breakdown()
    breakdown["overall_readiness"] = {
        "status": "website_only",
        "score": 72,
        "band": "fair",
        "weights": {"website": 1.0, "social": 0.0},
        "inputs": {"website_lead_gen": 72, "social": None},
    }
    result = _result(score_breakdown=breakdown, social_facts={}, social_score=None)

    payload = compose_report_payload(_job(), result)

    assert payload.social_audit is None
    assert payload.overall_readiness is not None
    assert payload.overall_readiness["score"] == 72


def test_ai_visibility_section_present_when_facts_in_breakdown() -> None:
    # The on-demand AI-visibility rerun stores facts in score_breakdown["ai_visibility"];
    # compose must surface them as the optional ReportPayload.ai_visibility section.
    breakdown = _score_breakdown()
    breakdown["ai_visibility"] = {
        "status": "complete",
        "provider": "semrush",
        "domain": "example.com",
        "visibility_score": 19,
        "visibility_band": "Low",
        "mentions": 28,
        "per_platform": [{"platform": "AI Overview", "mentions": 22, "share_pct": 78.6}],
    }
    payload = compose_report_payload(_job(), _result(score_breakdown=breakdown))
    assert payload.ai_visibility is not None
    assert payload.ai_visibility["visibility_score"] == 19
    assert payload.ai_visibility["per_platform"][0]["share_display"] == "78.6%"


def test_ai_visibility_section_absent_by_default() -> None:
    # A normal audit has no ai_visibility key => the section is None => report byte-identical.
    payload = compose_report_payload(_job(), _result())
    assert payload.ai_visibility is None


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        url="https://example.com/",
        niche="builder",
        target_audience="homeowners",
    )


def _result(**overrides) -> SimpleNamespace:
    payload = {
        "seo_score": 76,
        "uxui_score": 68,
        "lead_gen_score": 72,
        "crawled_pages": _crawled_pages(),
        "seo_facts": {},
        "uxui_facts": {},
        "external_seo_facts": _external_seo_facts(),
        "psi_facts": _missing_psi(),
        "score_breakdown": _score_breakdown(),
        "commentary": _commentary(),
        "validation_log": {
            "status": "complete",
            "numeric_claims_checked": 4,
            "unsupported_claim_count": 1,
            "action": "stripped_unsupported_numeric_sentences",
        },
        "report_metadata": {},
        "pdf_path": None,
        "rubric_version": "phase1-seo-v1+phase1-uxui-v1+phase1-composite-v1",
        "llm_model": "gpt-4o",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _crawled_pages() -> dict:
    return {
        "status": "partial",
        "requested_url": "https://example.com/",
        "final_url": "https://example.com/",
        "summary": {
            "successful_pages": 2,
            "failed_pages": 1,
            "skipped_pages": 1,
        },
        "failed_pages": [
            {
                "url": "https://example.com/gallery",
                "reason": "Timed out rendering https://example.com/gallery",
            }
        ],
        "skipped_pages": [
            {
                "url": "https://example.com/private",
                "reason": "disallowed_by_robots_txt",
            }
        ],
    }


def _missing_psi() -> dict:
    return {
        "status": "skipped",
        "reason": "missing_google_psi_api_key",
        "scope": "all_crawled_pages",
        "pages_requested": 2,
        "pages_analyzed": 0,
        "pages": [],
        "summary": {
            "avg_mobile_performance": None,
            "avg_desktop_performance": None,
            "complete_mobile_pages": 0,
            "complete_desktop_pages": 0,
            "slowest_pages": [],
        },
    }


def _external_seo_facts() -> dict:
    return {
        "status": "partial",
        "sources": {
            "screaming_frog": "complete",
            "gsc": "complete",
            "url_inspection": "skipped",
        },
        "screaming_frog": {
            "status": "complete",
            "source": "screaming_frog_csv",
            "summary": {
                "urls_crawled": 12,
                "client_error_internal_urls": 1,
                "non_indexable_internal_urls": 2,
            },
            "issues": [
                {
                    "id": "client_error_internal_urls",
                    "severity": "high",
                    "title": "Internal URLs returning 4xx errors",
                    "count": 1,
                    "examples": ["https://example.com/broken"],
                }
            ],
        },
        "gsc": {
            "status": "complete",
            "site_url": "sc-domain:example.com",
            "date_range": {"start": "2026-03-01", "end": "2026-05-29"},
            "summary": {
                "top_query_count": 1,
                "top_page_count": 1,
                "ranking_opportunities": 1,
                "high_impression_low_ctr_pages": 1,
                "declining_pages": 0,
            },
            "ranking_opportunities": [
                {
                    "query": "custom homes",
                    "clicks": 12,
                    "impressions": 300,
                    "ctr": 0.04,
                    "position": 8.2,
                }
            ],
            "high_impression_low_ctr_pages": [
                {
                    "page": "https://example.com/",
                    "clicks": 2,
                    "impressions": 500,
                    "ctr": 0.004,
                    "position": 6.1,
                }
            ],
            "declining_pages": [],
            "top_queries": [],
            "top_pages": [],
        },
        "url_inspection": {"status": "skipped", "summary": {}},
    }


def _score_breakdown() -> dict:
    return {
        "scores": {"seo": 76, "uxui": 68, "lead_gen": 72},
        "composite": {"weights": {"seo": 0.45, "uxui": 0.55}},
        "categories": {
            "seo": {
                "rules": [
                    _rule(
                        "seo.title.present_all_pages",
                        "Page titles are present.",
                        "pass",
                        76,
                        76,
                    ),
                    _rule(
                        "seo.meta_description.present_all_pages",
                        "Meta descriptions are present across crawled pages.",
                        "fail",
                        0,
                        24,
                    ),
                    _rule(
                        "seo.schema.present",
                        "Structured data can be read.",
                        "skipped",
                        0,
                        0,
                        weight=6,
                    ),
                ],
                "weights": {"evaluated": 100, "skipped": 6},
            },
            "uxui": {
                "rules": [
                    _rule(
                        "uxui.primary_cta.present",
                        "A primary CTA is present.",
                        "pass",
                        68,
                        68,
                    ),
                    _rule("uxui.forms.present", "The site includes a lead form.", "fail", 0, 32),
                ],
                "weights": {"evaluated": 100, "skipped": 0},
            },
        },
    }


def _rule(
    rule_id: str,
    description: str,
    result: str,
    awarded: float,
    possible: float,
    *,
    weight: float | None = None,
) -> dict:
    return {
        "rule_id": rule_id,
        "description": description,
        "result": result,
        "weight": possible if weight is None else weight,
        "points_awarded": awarded,
        "points_possible": possible,
        "evidence": {
            "value": awarded,
            "reason": None,
        },
    }


def _commentary() -> dict:
    return {
        "status": "complete",
        "provider": "openai",
        "model": "gpt-4o",
        "content": {
            "executive_summary": "The site has clear opportunities to improve lead capture.",
            "seo": _section("SEO needs stronger metadata", "seo"),
            "uxui": _section("UX/UI needs clearer conversion paths", "uxui"),
            "lead_generation": _section("Lead capture needs focus", "lead_generation"),
        },
    }


def _section(headline: str, evidence_prefix: str) -> dict:
    return {
        "headline": headline,
        "findings": [
            {
                "severity": "medium",
                "title": headline,
                "explanation": "The report finding is grounded in extracted audit facts.",
                "evidence_refs": [f"{evidence_prefix}.example"],
            }
        ],
        "recommendations": [
            {
                "tier": "quick_win",
                "title": f"Improve {headline}",
                "rationale": "This is the fastest visible improvement.",
                "action_items": ["Address the highest-confidence failed rule."],
            },
            {
                "tier": "mid_term",
                "title": f"Systematize {headline}",
                "rationale": "This improves repeatable lead generation.",
                "action_items": ["Roll the change across important pages."],
            },
            {
                "tier": "long_term",
                "title": f"Measure {headline}",
                "rationale": "This supports before-and-after comparison.",
                "action_items": ["Re-run the audit after implementation."],
            },
        ],
    }


def test_core_web_vitals_rates_lab_and_field_metrics() -> None:
    from apps.worker.stages.report_payload import _core_web_vitals

    psi_facts = {
        "status": "complete",
        "strategies": {
            "mobile": {
                "status": "complete",
                "lab_metrics": {
                    "first_contentful_paint_ms": 2900,
                    "largest_contentful_paint_ms": 3200,
                    "speed_index_ms": 4000,
                    "total_blocking_time_ms": 90,
                    "cumulative_layout_shift": 0.004,
                },
                "field_data": {
                    "origin": {
                        "overall_category": "AVERAGE",
                        "largest_contentful_paint_ms": {"p75": 3270, "category": "AVERAGE"},
                        "cumulative_layout_shift": {"p75": 0.03, "category": "FAST"},
                        "interaction_to_next_paint_ms": None,
                        "first_contentful_paint_ms": None,
                        "time_to_first_byte_ms": None,
                    },
                    "page": None,
                },
            },
            "desktop": {
                "status": "complete",
                "lab_metrics": {
                    "first_contentful_paint_ms": 1400,
                    "largest_contentful_paint_ms": 1900,
                    "speed_index_ms": 2100,
                    "total_blocking_time_ms": 20,
                    "cumulative_layout_shift": 0.002,
                },
                "field_data": {"page": None, "origin": None},
            },
        },
    }
    cwv = _core_web_vitals(psi_facts)

    assert cwv.available
    assert len(cwv.lab_rows) == 5
    lcp = next(r for r in cwv.lab_rows if r.label == "Largest Contentful Paint")
    assert lcp.mobile.value_label == "3.2 s"
    assert lcp.mobile.rating == "needs_improvement"
    assert lcp.desktop.value_label == "1.9 s"
    assert lcp.desktop.rating == "good"

    # Field data: origin-level, mobile, CrUX category authoritative; INP has no data.
    assert cwv.field_available
    assert cwv.field_source == "Whole site (origin)"
    assert cwv.field_assessment == "Needs improvement"
    field = {m.label: m for m in cwv.field_metrics}
    assert field["Largest Contentful Paint"].value_label == "3.3 s"
    assert field["Cumulative Layout Shift"].rating == "good"
    assert field["Interaction to Next Paint"].rating_label == "No data"


def test_core_web_vitals_absent_when_psi_skipped() -> None:
    from apps.worker.stages.report_payload import _core_web_vitals

    cwv = _core_web_vitals({"status": "skipped", "strategies": {}})
    assert not cwv.available
    assert not cwv.field_available
    assert cwv.lab_rows == []


def test_rate_limited_reason_has_client_facing_label() -> None:
    # site_health emits reason="rate_limited" on widespread throttling; the client-facing
    # report must render a human sentence, never the raw machine token.
    from apps.worker.stages.report_payload import SKIP_REASON_LABELS, _reason_label

    assert "rate_limited" in SKIP_REASON_LABELS
    assert _reason_label("rate_limited") != "rate_limited"


def test_fallback_findings_never_surface_skipped_rules() -> None:
    # A skipped rule was never measured: its positively-phrased description must not appear
    # as a "finding" (it reads as an asserted result), and no debug-speak explanation may
    # reach the client. An all-pass section gets an honest all-clear card instead.
    from apps.worker.stages.report_payload import RuleSummary, _fallback_findings

    skipped_only = [
        RuleSummary(
            rule_id="uxui.homepage_form.field_count",
            description="Homepage form length is practical for lead capture.",
            result="skipped",
        )
    ]
    findings = _fallback_findings("uxui", 100, skipped_only)
    assert len(findings) == 1
    assert findings[0].title == "No high-priority UX/UI issues were found"
    assert "skipped result" not in findings[0].explanation
    assert "practical for lead capture" not in findings[0].title

    # A genuinely evaluated fail/partial opportunity still surfaces as before.
    failed = [
        RuleSummary(
            rule_id="uxui.forms.present",
            description="Pages offer a lead capture form.",
            result="fail",
        )
    ]
    assert _fallback_findings("uxui", 60, failed)[0].title == "Pages offer a lead capture form."
