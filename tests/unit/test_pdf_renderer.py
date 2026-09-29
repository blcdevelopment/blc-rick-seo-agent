from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfReader

from apps.shared.config import Settings
from apps.worker.stages.pdf_renderer import render_report_pdf
from apps.worker.stages.report_payload import compose_report_payload


@pytest.mark.parametrize(
    ("variant", "extra_items", "missing_psi", "failed_pages"),
    [
        ("short", 0, False, False),
        ("medium", 8, False, False),
        ("long", 32, True, True),
    ],
)
def test_render_report_pdf_qa_variants(
    tmp_path,
    variant: str,
    extra_items: int,
    missing_psi: bool,
    failed_pages: bool,
) -> None:
    payload = compose_report_payload(
        _job(),
        _result(
            extra_items=extra_items,
            psi_facts=_missing_psi() if missing_psi else _complete_psi(),
            crawled_pages=_crawled_pages(failed_pages=failed_pages),
        ),
    )
    output_path = tmp_path / f"{variant}.pdf"

    pdf_result = render_report_pdf(payload, settings=_settings(tmp_path), output_path=output_path)

    assert output_path.exists()
    assert pdf_result.size_bytes > 10_000
    assert pdf_result.report_metadata["renderer"] == "weasyprint"
    assert pdf_result.report_metadata["brand_logo_used"] is True
    assert pdf_result.report_metadata["brand_logo_path"].endswith("assets/brand/blc-logo.svg")

    reader = PdfReader(str(output_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized_text = " ".join(text.split())
    assert len(reader.pages) == pdf_result.page_count
    assert "Website Audit Report" in text
    assert "Score Overview" not in text
    # ``.replace("- ", "-")`` collapses a hyphenation line-break: the score cards render
    # 3-up (narrower columns), so "business-readiness" can wrap at its hyphen and pypdf
    # extracts it as "business- readiness". The full phrase must still be present.
    assert "combined business-readiness score" in normalized_text.replace("- ", "-")
    assert "Formula: round((SEO 81 * 45%) + (UX/UI 74 * 55%)) = 77/100" in normalized_text
    assert "It evaluated 2 checks and earned 81 of 100 available points" in normalized_text
    assert "It evaluated 2 checks and earned 74 of 100 available points" in normalized_text
    assert "How to use it" in text
    assert "CTR (click-through rate)" in text
    assert "SEO rule trail" not in text
    assert "UX/UI rule trail" not in text
    assert "raw rule identifiers" in text
    # Visual-analytics section (charts) is appended at the end and renders in every variant.
    assert "Performance at a glance" in text
    assert "Rule health by category" in text
    assert "Page coverage" in text

    if variant == "long":
        assert len(reader.pages) >= 8
    if missing_psi:
        assert "No Google PageSpeed API key is configured" in normalized_text
    else:
        # Core Web Vitals (lab) + CrUX field snapshot render when PSI data is present.
        assert "Core Web Vitals" in normalized_text
        assert "Largest Contentful Paint" in normalized_text
        assert "Real-user field data (Chrome UX Report)" in normalized_text
    if failed_pages:
        assert "Timed out rendering" in text


def test_render_report_pdf_includes_advisory_accessibility_section(tmp_path) -> None:
    result = _result(
        extra_items=0,
        psi_facts=_complete_psi(),
        crawled_pages=_crawled_pages(failed_pages=False),
    )
    result.accessibility_facts = {
        "status": "complete",
        "axe_version": "4.10.2",
        "pages_scanned": 3,
        "impact_counts": {"critical": 1, "serious": 2, "moderate": 0, "minor": 0},
        "needs_review_count": 4,
        "disclaimer": (
            "This is an automated accessibility scan (axe-core), provided as advisory guidance "
            "rather than a compliance verdict."
        ),
        "notes": ["Document language and labels are evaluated in the scored checks above."],
        "issues": [
            {
                "rule_id": "color-contrast",
                "impact": "serious",
                "wcag_criteria": ["wcag143", "wcag2aa"],
                "help": "Elements must meet minimum colour contrast ratio thresholds",
                "help_url": "https://dequeuniversity.com/rules/axe/4.10/color-contrast",
                "instances": 12,
                "example_selectors": [".hero .tagline"],
                "example_pages": ["https://example.com/"],
                "failure_summary": "Fix the contrast ratio.",
            }
        ],
    }
    output_path = tmp_path / "a11y.pdf"

    render_report_pdf(
        compose_report_payload(_job(), result),
        settings=_settings(tmp_path),
        output_path=output_path,
    )

    text = " ".join((page.extract_text() or "") for page in PdfReader(str(output_path)).pages)
    normalized = " ".join(text.split())
    assert "Automated accessibility scan" in normalized
    assert "advisory guidance rather than a compliance verdict" in normalized
    assert "axe-core 4.10.2" in normalized
    assert "minimum colour contrast" in normalized
    # The advisory section does not disturb the scored content.
    assert "Formula: round((SEO 81 * 45%)" in normalized


def test_render_report_pdf_shows_opportunity_and_local_context(tmp_path) -> None:
    from apps.worker.stages.google_search_console import (
        _branded_split,
        _opportunity_estimate,
        _ranking_opportunities,
        _topic_clusters,
    )

    rows = [
        {
            "query": "custom home builder austin",
            "impressions": 3000,
            "clicks": 57,
            "ctr": 0.019,
            "position": 9,
        },
        {
            "query": "kitchen remodel cost austin",
            "impressions": 1200,
            "clicks": 48,
            "ctr": 0.04,
            "position": 6,
        },
        {
            "query": "builder lead converter",
            "impressions": 500,
            "clicks": 200,
            "ctr": 0.4,
            "position": 1,
        },
    ]
    opportunity = _opportunity_estimate(
        _ranking_opportunities(rows), window_days=90, site_total_clicks=305
    )
    result = _result(
        extra_items=0,
        psi_facts=_complete_psi(),
        crawled_pages=_crawled_pages(failed_pages=False),
    )
    result.external_seo_facts = {
        "status": "complete",
        "sources": {"technical_crawl": "skipped", "gsc": "complete", "url_inspection": "skipped"},
        "gsc": {
            "status": "complete",
            "site_url": "https://example.com/",
            "date_range": {"start": "2026-01-01", "end": "2026-03-31", "days": 90},
            "previous_date_range": {"start": "2025-10-03", "end": "2025-12-31", "days": 90},
            "summary": {
                "top_query_count": 3,
                "top_page_count": 0,
                "ranking_opportunities": 2,
                "high_impression_low_ctr_pages": 0,
                "declining_pages": 0,
            },
            "top_queries": rows,
            "top_pages": [],
            "ranking_opportunities": _ranking_opportunities(rows),
            "high_impression_low_ctr_pages": [],
            "declining_pages": [],
            "opportunity": opportunity,
            "branded": _branded_split(rows, "https://www.builderleadconverter.com/"),
            "topic_clusters": _topic_clusters(rows, "builderleadconverter"),
        },
    }
    output_path = tmp_path / "kpi.pdf"

    render_report_pdf(
        compose_report_payload(_job(), result),
        settings=_settings(tmp_path),
        output_path=output_path,
    )

    text = " ".join((page.extract_text() or "") for page in PdfReader(str(output_path)).pages)
    normalized = " ".join(text.split())
    # P1/P2 opportunity callout — assert the (non-uppercased) headline + a stored opportunity
    # number, so this proves the real GSC-derived figure reaches the page.
    assert "near-miss queries" in normalized
    assert str(opportunity["opportunity_clicks_low"]) in normalized
    assert "visits per month" in normalized
    assert "conservative" in normalized
    assert "A projection, not a promise" in normalized
    assert "Data window: 2026-01-01 to 2026-03-31" in normalized
    # P3 branded + P4 clusters
    assert "Branded vs non-branded" in normalized
    assert "Visibility by topic" in normalized
    # P6/P7 local context (always rendered)
    assert "Where your leads really come from" in normalized
    assert "local pack" in normalized
    assert "AI Overviews" in normalized


def test_render_report_pdf_omits_advisory_section_when_skipped(tmp_path) -> None:
    payload = compose_report_payload(
        _job(),
        _result(
            extra_items=0,
            psi_facts=_complete_psi(),
            crawled_pages=_crawled_pages(failed_pages=False),
        ),
    )
    output_path = tmp_path / "noa11y.pdf"

    render_report_pdf(payload, settings=_settings(tmp_path), output_path=output_path)

    text = " ".join((page.extract_text() or "") for page in PdfReader(str(output_path)).pages)
    assert "Automated accessibility scan" not in text


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        local_report_storage_dir=tmp_path,
        brand_config_path="brand/blc.yaml",
        report_template_path="templates/report.html",
        report_css_path="templates/report.css",
    )


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        url="https://example.com/",
        niche="builder",
        target_audience="homeowners",
    )


def _result(
    *,
    extra_items: int,
    psi_facts: dict,
    crawled_pages: dict,
) -> SimpleNamespace:
    return SimpleNamespace(
        seo_score=81,
        uxui_score=74,
        lead_gen_score=77,
        crawled_pages=crawled_pages,
        seo_facts={},
        uxui_facts={},
        psi_facts=psi_facts,
        score_breakdown=_score_breakdown(extra_items=extra_items),
        commentary=_commentary(extra_items=extra_items),
        validation_log={
            "status": "complete",
            "numeric_claims_checked": 6,
            "unsupported_claim_count": 0,
            "action": "none",
        },
        report_metadata={},
        pdf_path=None,
        rubric_version="phase1-seo-v1+phase1-uxui-v1+phase1-composite-v1",
        llm_model="gpt-4o",
    )


def _crawled_pages(*, failed_pages: bool) -> dict:
    failed = [
        {
            "url": "https://example.com/gallery",
            "reason": "Timed out rendering https://example.com/gallery",
        }
    ]
    return {
        "status": "partial" if failed_pages else "complete",
        "requested_url": "https://example.com/",
        "final_url": "https://example.com/",
        "summary": {
            "successful_pages": 3,
            "failed_pages": len(failed) if failed_pages else 0,
            "skipped_pages": 0,
        },
        "failed_pages": failed if failed_pages else [],
        "skipped_pages": [],
    }


def _missing_psi() -> dict:
    return {
        "status": "skipped",
        "reason": "missing_google_psi_api_key",
        "scope": "all_crawled_pages",
        "pages_requested": 3,
        "pages_analyzed": 0,
        "summary": {
            "avg_mobile_performance": None,
            "avg_desktop_performance": None,
            "complete_mobile_pages": 0,
            "complete_desktop_pages": 0,
            "slowest_pages": [],
        },
    }


def _complete_psi() -> dict:
    return {
        "status": "complete",
        "scope": "all_crawled_pages",
        "pages_requested": 3,
        "pages_analyzed": 3,
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
                        "first_contentful_paint_ms": {"p75": 2100, "category": "AVERAGE"},
                        "time_to_first_byte_ms": {"p75": 900, "category": "AVERAGE"},
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
        "summary": {
            "avg_mobile_performance": 72,
            "avg_desktop_performance": 91,
            "complete_mobile_pages": 3,
            "complete_desktop_pages": 3,
            "slowest_pages": [
                {
                    "url": "https://example.com/services",
                    "mobile_performance": 64,
                    "desktop_performance": 88,
                    "average_performance": 76,
                }
            ],
        },
    }


def _score_breakdown(*, extra_items: int) -> dict:
    seo_rules = [
        _rule("seo.title.present_all_pages", "Page titles are present.", "pass", 81, 81),
        _rule("seo.meta_description.present_all_pages", "Add stronger metadata.", "fail", 0, 19),
    ]
    uxui_rules = [
        _rule("uxui.primary_cta.present", "A primary CTA is present.", "pass", 74, 74),
        _rule("uxui.forms.present", "The site includes a lead form.", "fail", 0, 26),
    ]
    for index in range(extra_items):
        seo_rules.append(
            _rule(
                f"seo.synthetic.{index}",
                f"Synthetic SEO pagination rule {index}.",
                "skipped",
                0,
                0,
            )
        )
        uxui_rules.append(
            _rule(
                f"uxui.synthetic.{index}",
                f"Synthetic UX/UI pagination rule {index}.",
                "skipped",
                0,
                0,
            )
        )

    return {
        "scores": {"seo": 81, "uxui": 74, "lead_gen": 77},
        "composite": {"weights": {"seo": 0.45, "uxui": 0.55}},
        "categories": {
            "seo": {"rules": seo_rules, "weights": {"evaluated": 100, "skipped": 0}},
            "uxui": {"rules": uxui_rules, "weights": {"evaluated": 100, "skipped": 0}},
        },
    }


def _rule(rule_id: str, description: str, result: str, awarded: float, possible: float) -> dict:
    return {
        "rule_id": rule_id,
        "description": description,
        "result": result,
        "points_awarded": awarded,
        "points_possible": possible,
        "evidence": {"value": awarded, "reason": None},
    }


def _commentary(*, extra_items: int) -> dict:
    return {
        "status": "complete",
        "provider": "openai",
        "model": "gpt-4o",
        "content": {
            "executive_summary": "The site is close, but conversion clarity needs focus.",
            "seo": _section("SEO metadata needs refinement", "seo", extra_items),
            "uxui": _section("UX/UI conversion paths need refinement", "uxui", extra_items),
            "lead_generation": _section("Lead generation needs focus", "lead_generation", 0),
        },
    }


def _section(headline: str, evidence_prefix: str, extra_items: int) -> dict:
    findings = [
        {
            "severity": "medium",
            "title": headline,
            "explanation": "The finding is grounded in extracted audit evidence.",
            "evidence_refs": [f"{evidence_prefix}.example"],
        }
    ]
    recommendations = [
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
    ]
    for index in range(extra_items):
        findings.append(
            {
                "severity": "medium",
                "title": f"{headline} finding {index}",
                "explanation": "Additional pagination QA finding grounded in audit facts.",
                "evidence_refs": [f"{evidence_prefix}.synthetic.{index}"],
            }
        )
        recommendations.append(
            {
                "tier": "quick_win" if index % 3 == 0 else "mid_term",
                "title": f"{headline} recommendation {index}",
                "rationale": "Additional pagination QA recommendation.",
                "action_items": ["Keep the page-break behavior stable."],
            }
        )
    return {
        "headline": headline,
        "findings": findings,
        "recommendations": recommendations,
    }
