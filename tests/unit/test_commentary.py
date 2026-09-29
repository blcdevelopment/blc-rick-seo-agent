from apps.shared.config import Settings
from apps.worker.stages.commentary import (
    CommentaryContent,
    commentary_json_schema,
    generate_commentary,
)


def _rule(
    rule_id: str,
    description: str,
    result: str,
    *,
    weight: float,
    impact: str,
    tier: str,
    label: str,
    fact_path: str,
    value: object,
) -> dict:
    return {
        "rule_id": rule_id,
        "description": description,
        "weight": weight,
        "evaluator": "threshold",
        "fact_path": fact_path,
        "impact": impact,
        "tier": tier,
        "finding_label": label,
        "remediation": f"Resolve: {label}.",
        "surface_as_finding": True,
        "result": result,
        "points_awarded": 0.0,
        "points_possible": weight,
        "evidence": {"value": value, "params": {}, "reason": None},
    }


def _score_breakdown() -> dict:
    return {
        "status": "complete",
        "rubric_version": "phase1-seo-v2+phase2-uxui-v3+phase1-composite-v1",
        "scores": {"seo": 42, "uxui": 38, "lead_gen": 40},
        "categories": {
            "seo": {
                "rules": [
                    _rule(
                        "seo.meta_description.present_all_pages",
                        "Meta descriptions are present across crawled pages.",
                        "fail",
                        weight=10,
                        impact="high",
                        tier="quick_win",
                        label="Meta descriptions are missing on some pages",
                        fact_path="seo.summary.meta_descriptions_present_pct",
                        value=0.0,
                    ),
                ]
            },
            "uxui": {
                "rules": [
                    _rule(
                        "uxui.trust.present",
                        "Trust signals are visible on at least one crawled page.",
                        "partial",
                        weight=9,
                        impact="high",
                        tier="mid_term",
                        label="Trust signals are limited",
                        fact_path="uxui.summary.pages_with_trust_signals",
                        value=1,
                    ),
                ]
            },
        },
        "composite": {"score": 40},
    }


def _context() -> dict:
    return {"url": "https://example.com/", "niche": "builder", "target_audience": "homeowners"}


def test_commentary_schema_is_json_object() -> None:
    schema = commentary_json_schema()

    assert schema["type"] == "object"
    assert "executive_summary" in schema["properties"]


def test_generate_commentary_is_deterministic_without_api_key() -> None:
    commentary = generate_commentary(
        audit_context=_context(),
        seo_facts={"summary": {"meta_descriptions_present_pct": 0.0}},
        uxui_facts={"summary": {"pages_with_trust_signals": 1}},
        psi_facts={},
        score_breakdown=_score_breakdown(),
        settings=Settings(_env_file=None, openai_api_key=None),
    )

    assert commentary["status"] == "deterministic"
    assert commentary["provider"] == "deterministic"
    content = CommentaryContent.model_validate(commentary["content"])

    # The failing high-impact SEO rule surfaces as a high-severity quick-win finding,
    # with no internal rule IDs in the user-facing text.
    seo = content.seo
    assert seo.findings, "expected at least one SEO finding"
    assert seo.findings[0].severity == "high"
    assert seo.findings[0].title == "Meta descriptions are missing on some pages"
    assert "seo.meta_description" not in seo.findings[0].title
    assert seo.recommendations[0].tier == "quick_win"

    # partial + high impact -> medium severity (one notch down).
    assert content.uxui.findings[0].severity == "medium"


def test_generate_commentary_is_deterministic_even_with_api_key() -> None:
    # Phase 1 does not call OpenAI: the deterministic plan is the report regardless of key.
    # (Phase 2 will add a structure-locked polish layer behind this key check.)
    def run() -> dict:
        return generate_commentary(
            audit_context=_context(),
            seo_facts={"summary": {"meta_descriptions_present_pct": 0.0}},
            uxui_facts={"summary": {"pages_with_trust_signals": 1}},
            psi_facts={},
            score_breakdown=_score_breakdown(),
            settings=Settings(_env_file=None, openai_api_key="test-key"),
        )

    first = run()
    second = run()

    assert first["status"] == "deterministic"
    assert first["provider"] == "deterministic"
    # Identical facts -> byte-identical commentary content across runs.
    assert first["content"] == second["content"]
