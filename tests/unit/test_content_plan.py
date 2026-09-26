from pathlib import Path

from apps.shared.config import Settings
from apps.worker.stages.commentary import CommentaryContent
from apps.worker.stages.content_plan import build_content_plan
from apps.worker.stages.extractor_seo import extract_seo_facts
from apps.worker.stages.extractor_uxui import extract_uxui_facts
from apps.worker.stages.grounding_validator import validate_commentary_grounding
from apps.worker.stages.scoring import score_audit

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, openai_api_key=None, **kwargs)


def _rule(
    rule_id: str,
    result: str,
    *,
    weight: float,
    impact: str,
    tier: str,
    surface: bool = True,
    label: str | None = None,
    fact_path: str = "seo.summary.value",
    value: object = None,
    merged_into: str | None = None,
) -> dict:
    return {
        "rule_id": rule_id,
        "description": f"desc {rule_id}",
        "weight": weight,
        "evaluator": "threshold",
        "fact_path": fact_path,
        "impact": impact,
        "tier": tier,
        "finding_label": label or f"Problem {rule_id}",
        "remediation": f"Fix {rule_id}.",
        "surface_as_finding": surface,
        "merged_into": merged_into,
        "result": result,
        "points_awarded": 0.0,
        "points_possible": weight,
        "evidence": {"value": value, "params": {}, "reason": None},
    }


def _breakdown(seo_rules: list[dict], uxui_rules: list[dict] | None = None) -> dict:
    return {
        "scores": {"seo": 40, "uxui": 40, "lead_gen": 40},
        "categories": {"seo": {"rules": seo_rules}, "uxui": {"rules": uxui_rules or []}},
        "composite": {"score": 40},
    }


def _plan(breakdown: dict, **settings_kwargs) -> CommentaryContent:
    return build_content_plan(
        audit_context={"url": "https://x.test", "niche": None, "target_audience": None},
        seo_facts={},
        uxui_facts={},
        psi_facts={},
        score_breakdown=breakdown,
        settings=_settings(**settings_kwargs),
    )


def test_only_failing_surfaced_rules_become_findings() -> None:
    plan = _plan(
        _breakdown(
            [
                _rule("seo.a", "pass", weight=5, impact="high", tier="quick_win"),
                _rule("seo.b", "skipped", weight=5, impact="high", tier="quick_win"),
                _rule("seo.meta", "fail", weight=10, impact="high", tier="quick_win", label="Meta"),
                _rule(
                    "seo.hidden", "fail", weight=10, impact="high", tier="quick_win", surface=False
                ),
            ]
        )
    )
    assert [f.title for f in plan.seo.findings] == ["Meta"]


def test_findings_ordered_by_severity_then_weight_then_id() -> None:
    plan = _plan(
        _breakdown(
            [
                _rule("seo.low_w", "fail", weight=6, impact="high", tier="quick_win", label="A"),
                _rule("seo.high_w", "fail", weight=9, impact="high", tier="quick_win", label="B"),
                _rule(
                    "seo.medium", "fail", weight=20, impact="medium", tier="quick_win", label="C"
                ),
            ]
        )
    )
    # High severity before medium (even though C has the largest weight); within high,
    # heavier weight first.
    assert [(f.severity, f.title) for f in plan.seo.findings] == [
        ("high", "B"),
        ("high", "A"),
        ("medium", "C"),
    ]


def test_severity_matrix_partial_is_one_notch_down() -> None:
    plan = _plan(
        _breakdown(
            [
                _rule(
                    "seo.a", "fail", weight=5, impact="high", tier="quick_win", label="fail-high"
                ),
                _rule(
                    "seo.b", "partial", weight=5, impact="high", tier="quick_win", label="part-high"
                ),
                _rule(
                    "seo.c", "partial", weight=5, impact="low", tier="quick_win", label="part-low"
                ),
            ]
        )
    )
    by_title = {f.title: f.severity for f in plan.seo.findings}
    assert by_title["fail-high"] == "high"
    assert by_title["part-high"] == "medium"
    assert by_title["part-low"] == "info"


def test_tier_comes_from_rule_metadata() -> None:
    plan = _plan(
        _breakdown([_rule("seo.a", "fail", weight=5, impact="high", tier="long_term", label="A")])
    )
    assert plan.seo.recommendations[0].tier == "long_term"


def test_top_n_truncation_is_deterministic() -> None:
    rules = [
        _rule(f"seo.r{i}", "fail", weight=5, impact="high", tier="quick_win", label=f"R{i}")
        for i in range(10)
    ]
    plan = _plan(
        _breakdown(rules),
        commentary_max_findings_per_section=3,
    )
    assert len(plan.seo.findings) == 3
    # Recommendations pair 1:1 with the rendered findings — a finding without its fix
    # reads as unanswered, so the findings cap governs both lists.
    assert len(plan.seo.recommendations) == 3
    # Lowest rule_id wins ties deterministically.
    assert [f.title for f in plan.seo.findings] == ["R0", "R1", "R2"]


def test_no_internal_rule_ids_leak_into_prose() -> None:
    plan = _plan(
        _breakdown(
            [
                _rule(
                    "seo.meta_description.x",
                    "fail",
                    weight=9,
                    impact="high",
                    tier="quick_win",
                    label="Meta missing",
                )
            ]
        )
    )
    finding = plan.seo.findings[0]
    assert "seo.meta_description" not in finding.title
    assert "seo.meta_description" not in finding.meaning
    assert "seo.meta_description" not in finding.why


def test_external_seo_findings_include_real_locations() -> None:
    rule = _rule(
        "seo.technical_crawl.no_broken_internal_urls",
        "fail",
        weight=8,
        impact="high",
        tier="quick_win",
        label="Internal broken URLs were found",
        fact_path="external_seo.technical_crawl.summary.client_error_internal_urls",
        value=2,
    )
    plan = build_content_plan(
        audit_context={"url": "https://x.test", "niche": None, "target_audience": None},
        seo_facts={},
        uxui_facts={},
        psi_facts={},
        external_seo_facts={
            "technical_crawl": {
                "status": "complete",
                "issues": [
                    {
                        "id": "client_error_internal_urls",
                        "examples": ["https://example.com/broken"],
                    }
                ],
            }
        },
        score_breakdown=_breakdown([rule]),
        settings=_settings(),
    )

    finding = plan.seo.findings[0]
    assert "https://example.com/broken" in finding.location_urls
    assert "external_seo" not in finding.meaning
    assert "external_seo" not in finding.why


def test_executive_summary_leads_with_grounded_opportunity_when_gsc_present() -> None:
    from apps.worker.stages.google_search_console import (
        _opportunity_estimate,
        _ranking_opportunities,
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
    ]
    opportunity = _opportunity_estimate(
        _ranking_opportunities(rows), window_days=91, site_total_clicks=105
    )
    external_seo_facts = {
        "status": "complete",
        "gsc": {"status": "complete", "opportunity": opportunity},
    }
    breakdown = _breakdown(
        [
            _rule(
                "seo.meta_description.present_all_pages",
                "fail",
                weight=10,
                impact="high",
                tier="quick_win",
            )
        ]
    )

    plan = build_content_plan(
        audit_context={"url": "https://x.test", "niche": None, "target_audience": None},
        seo_facts={},
        uxui_facts={},
        psi_facts={},
        external_seo_facts=external_seo_facts,
        score_breakdown=breakdown,
        settings=_settings(),
    )
    # The summary now LEADS with the business outcome, not the score — windowed,
    # reconciled against total site clicks, and framed as a conservative scenario.
    assert plan.executive_summary.startswith("Based on your last 91 days")
    assert "visits a month" in plan.executive_summary
    assert "near-miss queries" in plan.executive_summary
    assert "not a promise" in plan.executive_summary
    assert "Lead Generation Readiness" in plan.executive_summary  # score still present, demoted

    # Every opportunity number is a stored GSC fact, so grounding strips nothing.
    commentary = {"status": "deterministic", "content": plan.model_dump(mode="json")}
    sanitized, log = validate_commentary_grounding(
        commentary,
        fact_sources={
            "external_seo_facts": external_seo_facts,
            "scores": breakdown["scores"],
        },
    )
    assert log["unsupported_claim_count"] == 0
    assert sanitized["content"]["executive_summary"] == plan.executive_summary


def test_executive_summary_unchanged_without_gsc_opportunity() -> None:
    # No GSC => no opportunity lead-in => the legacy score-led summary, byte-for-byte.
    breakdown = _breakdown(
        [
            _rule(
                "seo.meta_description.present_all_pages",
                "fail",
                weight=10,
                impact="high",
                tier="quick_win",
            )
        ]
    )
    plan = _plan(breakdown)
    assert not plan.executive_summary.startswith("Your site already appears")
    assert plan.executive_summary.startswith("This audit scored the site")


def test_plan_is_grounding_safe_on_real_fixture() -> None:
    settings = _settings()
    html = (FIXTURE_DIR / "weak_site.html").read_text(encoding="utf-8")
    pages = [
        {
            "url": "https://weak.example/",
            "final_url": "https://weak.example/",
            "status_code": 200,
            "html": html,
        }
    ]
    seo_facts = extract_seo_facts(pages)
    uxui_facts = extract_uxui_facts(pages)
    psi_facts = {"status": "skipped", "summary": {}}
    breakdown = score_audit(seo_facts, uxui_facts, psi_facts, settings)

    plan = build_content_plan(
        audit_context={"url": "https://weak.example/", "niche": None, "target_audience": None},
        seo_facts=seo_facts,
        uxui_facts=uxui_facts,
        psi_facts=psi_facts,
        score_breakdown=breakdown,
        settings=settings,
    )
    commentary = {
        "status": "deterministic",
        "provider": "deterministic",
        "model": "deterministic",
        "content": plan.model_dump(mode="json"),
    }

    sanitized, log = validate_commentary_grounding(
        commentary,
        fact_sources={
            "seo_facts": seo_facts,
            "uxui_facts": uxui_facts,
            "psi_facts": psi_facts,
            "scores": breakdown["scores"],
        },
    )

    # Every number the deterministic plan emits is a stored fact or a score, so grounding
    # strips nothing and the content survives byte-identical.
    assert log["unsupported_claim_count"] == 0
    assert sanitized["content"] == commentary["content"]
    # The weak fixture must actually produce findings, or the invariant above is vacuous.
    content = CommentaryContent.model_validate(sanitized["content"])
    assert content.seo.findings or content.uxui.findings


def test_recommendation_title_states_the_fix_not_the_problem() -> None:
    """A recommendation headline must describe the FIX, never restate the finding.

    Regression: report cards showed the same sentence as both the problem and the
    recommendation (e.g. "Pages do not use a single clear H1 heading" twice).
    """
    plan = _plan(
        _breakdown(
            [
                _rule(
                    "seo.h1.present_once",
                    "fail",
                    weight=9,
                    impact="medium",
                    tier="quick_win",
                    label="Pages do not use a single clear H1 heading",
                )
            ]
        )
    )
    finding = plan.seo.findings[0]
    rec = plan.seo.recommendations[0]
    assert finding.title == "Pages do not use a single clear H1 heading"
    assert rec.title == "Give every page one clear H1 heading"
    assert rec.title != finding.title


def test_every_surfaceable_rubric_rule_has_an_action_title() -> None:
    """No surfacing rule may fall back to restating its problem as the fix."""
    import yaml

    from apps.worker.stages.content_plan import _ACTION_TITLES

    root = Path(__file__).resolve().parents[2]
    for name in ("seo.yaml", "uxui.yaml"):
        data = yaml.safe_load((root / "rubrics" / name).read_text())
        for rule in data["rules"]:
            if rule.get("surface_as_finding") is False:
                continue
            assert rule["id"] in _ACTION_TITLES, f"{name}: no action title for {rule['id']}"


def test_action_titles_carry_no_grounding_strippable_numbers() -> None:
    """Numeric claims in a title get stripped/flagged by grounding; titles must be
    number-free (the exact targets live in the grounding-exempt remediation)."""
    import re

    from apps.worker.stages.content_plan import _ACTION_TITLES

    # Mirror grounding_validator.NUMERIC_RE: a digit not preceded by a letter
    # (so "H1" is fine, "30-65" is not).
    numeric = re.compile(r"(?<![A-Za-z])[-+]?\d")
    offenders = {rid: title for rid, title in _ACTION_TITLES.items() if numeric.search(title)}
    assert not offenders, f"numeric titles will be grounding-stripped: {offenders}"


def test_range_finding_states_measured_value_and_baseline() -> None:
    """A "length is outside the ideal range" finding must state the measured value AND
    the baseline range it was judged against — and every number must survive grounding."""
    from apps.worker.stages.grounding_validator import validate_commentary_grounding

    seo_facts = {
        "status": "complete",
        "pages": [
            {
                "url": "https://x.test/",
                "meta_description": {
                    "length": 240,
                    "ideal_min_length": 70,
                    "ideal_max_length": 160,
                    "is_reasonable_length": False,
                },
            }
        ],
    }
    rule = _rule(
        "seo.homepage_meta_description.reasonable_length",
        "fail",
        weight=8,
        impact="medium",
        tier="quick_win",
        label="Homepage meta description length is outside the ideal range",
        fact_path="seo.pages[0].meta_description.is_reasonable_length",
        value=False,
    )
    plan = build_content_plan(
        audit_context={"url": "https://x.test", "niche": None, "target_audience": None},
        seo_facts=seo_facts,
        uxui_facts={},
        psi_facts={},
        score_breakdown=_breakdown([rule]),
        settings=_settings(),
    )

    meaning = plan.seo.findings[0].meaning
    assert "240 characters" in meaning
    assert "70 to 160 characters" in meaning
    assert "longer than" in meaning

    # The measured value and the baseline bounds are all stored facts, so grounding keeps them.
    commentary = {
        "status": "deterministic",
        "provider": "deterministic",
        "model": "deterministic",
        "content": plan.model_dump(mode="json"),
    }
    sanitized, log = validate_commentary_grounding(
        commentary,
        fact_sources={
            "seo_facts": seo_facts,
            "uxui_facts": {},
            "psi_facts": {},
            # _breakdown() scores the plan at 40; the grounding sources must agree, or the
            # summary's own score numbers are (correctly) flagged.
            "scores": {"seo": 40, "uxui": 40, "lead_gen": 40},
        },
    )
    assert log["unsupported_claim_count"] == 0
    assert "240 characters" in sanitized["content"]["seo"]["findings"][0]["meaning"]


def test_every_surfaced_finding_carries_its_fix_including_long_term() -> None:
    # Six quick-win rules plus one heavy long_term PageSpeed rule: severity-first
    # selection keeps the PageSpeed finding, and the paired recommendation keeps its fix
    # in the section AND the roadmap — the old tier-first sort dropped it past the cap.
    rules = [
        _rule(f"seo.r{i}", "fail", weight=5, impact="medium", tier="quick_win", label=f"R{i}")
        for i in range(6)
    ]
    rules.append(
        _rule(
            "seo.psi.mobile_performance",
            "partial",
            weight=10,
            impact="high",
            tier="long_term",
            label="Mobile page performance needs improvement",
        )
    )
    plan = _plan(_breakdown(rules))
    finding_titles = [f.title for f in plan.seo.findings]
    rec_titles = [r.title for r in plan.seo.recommendations]
    assert "Mobile page performance needs improvement" in finding_titles
    assert "Speed up page loads on mobile" in rec_titles
    assert len(rec_titles) == len(finding_titles)


def test_overlapping_rules_merge_into_one_card() -> None:
    # H1 + heading-outline and on-page alt + technical-crawl alt each collapse into one
    # card with a covered-by note; scores are untouched (presentation-only merge).
    rules = [
        _rule(
            "seo.h1.present_once",
            "fail",
            weight=8,
            impact="medium",
            tier="quick_win",
            label="Pages do not use a single clear H1 heading",
        ),
        _rule(
            "seo.aeo.heading_hierarchy",
            "partial",
            weight=3,
            impact="low",
            tier="quick_win",
            label="Heading outline is broken",
            merged_into="seo.h1.present_once",
        ),
        _rule(
            "seo.images.alt_coverage",
            "fail",
            weight=6,
            impact="medium",
            tier="quick_win",
            label="Image alt-text coverage is low",
        ),
        _rule(
            "seo.technical_crawl.missing_image_alt",
            "fail",
            weight=4,
            impact="medium",
            tier="quick_win",
            label="Images missing alt text",
            merged_into="seo.images.alt_coverage",
        ),
    ]
    plan = _plan(_breakdown(rules))
    titles = [f.title for f in plan.seo.findings]
    assert len(titles) == 2
    h1_card = next(f for f in plan.seo.findings if "H1" in f.title)
    assert "Heading outline is broken" in h1_card.why
    alt_card = next(f for f in plan.seo.findings if "alt-text" in f.title)
    assert "Images missing alt text" in alt_card.why


def test_malformed_stored_merge_metadata_never_drops_a_finding() -> None:
    # Rubric load now rejects self-merges/chains, but OLD stored breakdowns could still carry
    # malformed merge metadata — a diverted rule whose primary never renders must be re-kept,
    # not silently dropped from the client report.
    rules = [
        _rule(
            "seo.a",
            "fail",
            weight=5,
            impact="high",
            tier="quick_win",
            label="Self-merged rule",
            merged_into="seo.a",
        )
    ]
    plan = _plan(_breakdown(rules))
    assert [f.title for f in plan.seo.findings] == ["Self-merged rule"]


def test_secondary_rule_alone_still_surfaces() -> None:
    solo = _plan(
        _breakdown(
            [
                _rule(
                    "seo.aeo.heading_hierarchy",
                    "partial",
                    weight=3,
                    impact="low",
                    tier="quick_win",
                    label="Heading outline is broken",
                    merged_into="seo.h1.present_once",
                )
            ]
        )
    )
    assert [f.title for f in solo.seo.findings] == ["Heading outline is broken"]


def _alt_pair_with_crowd() -> list[dict]:
    """A partial primary (severity low) + fail secondary (severity medium) alt pair,
    crowded by rules that push a low-severity card past the findings cap of 5."""
    return [
        _rule("seo.a", "fail", weight=9, impact="high", tier="quick_win", label="High A"),
        _rule("seo.b", "fail", weight=8, impact="high", tier="quick_win", label="High B"),
        _rule("seo.c", "fail", weight=7, impact="high", tier="quick_win", label="High C"),
        _rule("seo.d", "fail", weight=7, impact="medium", tier="quick_win", label="Medium D"),
        _rule("seo.e", "fail", weight=7, impact="low", tier="quick_win", label="Low E"),
        _rule(
            "seo.images.alt_coverage",
            "partial",
            weight=6,
            impact="medium",
            tier="quick_win",
            label="Image alt-text coverage is low",
        ),
        _rule(
            "seo.technical_crawl.missing_image_alt",
            "fail",
            weight=4,
            impact="medium",
            tier="quick_win",
            label="Images missing alt text",
            merged_into="seo.images.alt_coverage",
        ),
    ]


def test_merged_card_adopts_group_severity_and_survives_the_cap() -> None:
    # The fail secondary (medium severity) folds into a partial primary (low severity).
    # The merged card must rank and read at the group's strongest severity — otherwise
    # it sinks below the findings cap and the issue vanishes from the report entirely.
    plan = _plan(_breakdown(_alt_pair_with_crowd()))
    titles = [f.title for f in plan.seo.findings]
    assert "Image alt-text coverage is low" in titles
    alt_card = next(f for f in plan.seo.findings if "alt-text" in f.title)
    assert alt_card.severity == "medium"
    assert "Images missing alt text" in alt_card.why
    # The absorbed secondary's issue is represented; the recommendation travels with it.
    rec_titles = [r.title for r in plan.seo.recommendations]
    assert any("alt" in title.lower() for title in rec_titles)


def test_top_priority_label_names_a_rendered_card() -> None:
    # With only the alt pair surfaced, the unmerged secondary outranks its primary; the
    # executive summary must cite the card the findings section actually shows.
    rules = [
        _rule(
            "seo.images.alt_coverage",
            "partial",
            weight=6,
            impact="medium",
            tier="quick_win",
            label="Image alt-text coverage is low",
        ),
        _rule(
            "seo.technical_crawl.missing_image_alt",
            "fail",
            weight=4,
            impact="medium",
            tier="quick_win",
            label="Images missing alt text",
            merged_into="seo.images.alt_coverage",
        ),
    ]
    plan = _plan(_breakdown(rules))
    assert "Image alt-text coverage is low" in plan.executive_summary
    assert "Images missing alt text" not in plan.executive_summary
    assert [f.title for f in plan.seo.findings] == ["Image alt-text coverage is low"]


def test_lead_in_zero_leads_and_zero_click_site_copy() -> None:
    from apps.worker.stages.content_plan import _opportunity_lead_in

    base = {
        "window_days": 91,
        "site_monthly_clicks": 0,
        "total_striking_impressions": 40,
        "striking_query_count": 3,
        "modeled_query_count": 3,
        "striking_position_min": 4,
        "striking_position_max": 20,
        "opportunity_clicks_low": 2,
        "opportunity_clicks_high": 4,
        "estimated_leads_low": 0,
        "estimated_leads_high": 0,
        "lead_rate_low_pct": 5,
        "lead_rate_high_pct": 10,
    }
    text = _opportunity_lead_in(base)
    # No dangling pronoun without the site-clicks line, and no "0 to 0 extra inquiries".
    assert "searchers see the site" in text
    assert "inquiries" not in text
    assert "visits a month." in text
    # A conservative range that rounds to zero suppresses the lead-in entirely.
    assert _opportunity_lead_in({**base, "opportunity_clicks_high": 0}) == ""
