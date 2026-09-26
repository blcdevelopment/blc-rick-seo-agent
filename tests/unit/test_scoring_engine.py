import json
from pathlib import Path

from apps.shared.config import Settings
from apps.worker.stages.extractor_seo import extract_seo_facts
from apps.worker.stages.extractor_uxui import extract_uxui_facts
from apps.worker.stages.scoring import (
    Rubric,
    RubricRule,
    load_composite_rubric,
    load_rubric,
    score_audit,
    score_category,
)


def test_data_sufficient_distinguishes_all_skip_from_genuine_zero() -> None:
    # Every rule skips (fact absent + skip_if_missing): score is 0 only because nothing was
    # scorable, so data_sufficient must be False (not indistinguishable from a real 0).
    all_skip = Rubric(
        version="t-skip",
        category="seo",
        max_score=100,
        normalization="rescale_to_max",
        rules=[
            RubricRule(
                id="seo.absent",
                description="needs a fact that is absent",
                weight=10,
                fact_path="seo.summary.absent",
                evaluator="boolean",
                skip_if_missing=True,
            )
        ],
    )
    out = score_category({"seo": {"summary": {}}}, all_skip)
    assert out["score"] == 0
    assert out["data_sufficient"] is False

    # A scorable rule => data_sufficient True and a real score.
    scored = Rubric(
        version="t-ok",
        category="seo",
        max_score=100,
        normalization="rescale_to_max",
        rules=[
            RubricRule(
                id="seo.present",
                description="present fact",
                weight=10,
                fact_path="seo.summary.present",
                evaluator="boolean",
            )
        ],
    )
    out2 = score_category({"seo": {"summary": {"present": True}}}, scored)
    assert out2["score"] == 100
    assert out2["data_sufficient"] is True


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _page(name: str, html: str) -> dict:
    return {
        "url": f"https://{name}.example/",
        "final_url": f"https://{name}.example/",
        "status_code": 200,
        "html": html,
    }


def _psi(mobile: int | None = None, desktop: int | None = None) -> dict:
    return {
        "status": "complete" if mobile is not None and desktop is not None else "skipped",
        "summary": {
            "avg_mobile_performance": mobile,
            "avg_desktop_performance": desktop,
        },
    }


def _facts_for_fixture(name: str) -> tuple[dict, dict]:
    html = _load_fixture(name)
    pages = [_page(name.removesuffix("_site.html"), html)]
    return extract_seo_facts(pages), extract_uxui_facts(pages)


def test_phase_1_rubrics_load_and_validate() -> None:
    settings = Settings(_env_file=None)

    seo = load_rubric(settings.rubric_seo_path)
    uxui = load_rubric(settings.rubric_uxui_path)
    composite = load_composite_rubric(settings.rubric_composite_path)

    assert seo.version == "phase2-seo-v12"
    assert uxui.version == "phase2-uxui-v3"
    assert sum(rule.weight for rule in seo.rules) == 261
    assert sum(rule.weight for rule in uxui.rules) == 100
    assert composite.weights == {"seo": 0.45, "uxui": 0.55}


def test_shipped_seo_rubric_declares_the_merge_pairs() -> None:
    # The presentation-level merges live in the rubric (merged_into), not in code: a rule
    # rename/split must update the pair in the same file (a dangling target fails rubric
    # load via Rubric._merged_into_targets_exist — exercised by loading here).
    settings = Settings(_env_file=None)
    seo = load_rubric(settings.rubric_seo_path)
    merges = {rule.id: rule.merged_into for rule in seo.rules if rule.merged_into}
    assert merges == {
        "seo.aeo.heading_hierarchy": "seo.h1.present_once",
        "seo.technical_crawl.missing_h1": "seo.h1.present_once",
        "seo.technical_crawl.missing_image_alt": "seo.images.alt_coverage",
    }


def _merge_rule(rule_id: str, merged_into: str | None = None) -> dict:
    rule = {
        "id": rule_id,
        "description": rule_id,
        "weight": 1,
        "fact_path": f"seo.summary.{rule_id.split('.')[-1]}",
        "evaluator": "boolean",
    }
    if merged_into:
        rule["merged_into"] = merged_into
    return rule


def test_rubric_with_dangling_merge_target_fails_to_load() -> None:
    import pytest
    from pydantic import ValidationError

    from apps.worker.stages.scoring import Rubric

    with pytest.raises(ValidationError, match="unknown rule id"):
        Rubric.model_validate(
            {
                "version": "test-v1",
                "category": "seo",
                "rules": [_merge_rule("seo.a", merged_into="seo.does_not_exist")],
            }
        )


def test_rubric_with_self_merge_or_chain_fails_to_load() -> None:
    # A self-merge or a merge chain silently DROPS a finding from every client report
    # (the rule is diverted into a covered-by group that never renders), so both are
    # hard load-time errors, not silent config hazards.
    import pytest
    from pydantic import ValidationError

    from apps.worker.stages.scoring import Rubric

    with pytest.raises(ValidationError, match="cannot merge into itself"):
        Rubric.model_validate(
            {
                "version": "test-v1",
                "category": "seo",
                "rules": [_merge_rule("seo.a", merged_into="seo.a")],
            }
        )
    with pytest.raises(ValidationError, match="must be terminal"):
        Rubric.model_validate(
            {
                "version": "test-v1",
                "category": "seo",
                "rules": [
                    _merge_rule("seo.a", merged_into="seo.b"),
                    _merge_rule("seo.b", merged_into="seo.c"),
                    _merge_rule("seo.c"),
                ],
            }
        )


def test_v3_renamed_rules_skip_on_pre_v3_stored_facts() -> None:
    # Rerun-enrichment rescores STORED uxui facts with the current rubric. Facts stored
    # by the v2 extractor have neither pages_with_form_capture nor pages_with_contact_path;
    # those rules must rescale out (skip), not flip a previously-passing audit to
    # "No lead capture form was found".
    settings = Settings(_env_file=None)
    rubric = load_rubric(settings.rubric_uxui_path)
    legacy_facts = {
        "uxui": {
            "status": "complete",
            "summary": {
                "pages_with_primary_cta": 1,
                "total_ctas": 4,
                "above_fold_ctas": 1,
                "pages_with_form": 1,  # the v2 key
                "pages_with_phone": 1,
                "pages_with_email": 1,  # the v2 key
                "pages_with_trust_signals": 1,
                "total_trust_signals": 3,
            },
            "pages": [
                {
                    "navigation": {"has_nav": True},
                    "content": {"has_substantial_copy": True},
                    "forms": {"total_field_count": 4},
                    "lead_capture": {"has_direct_contact": True, "has_cta": True},
                }
            ],
        }
    }
    out = score_category(legacy_facts, rubric)
    by_id = {rule["rule_id"]: rule for rule in out["rules"]}
    assert by_id["uxui.forms.present"]["result"] == "skipped"
    assert by_id["uxui.contact_path.low_pressure"]["result"] == "skipped"
    # Nothing fabricated a failure; the category rescales around the missing facts.
    assert not any(
        rule["result"] == "fail"
        and rule["rule_id"] in {"uxui.forms.present", "uxui.contact_path.low_pressure"}
        for rule in out["rules"]
    )


def test_scoring_calibrates_strong_and_weak_fixture_sites() -> None:
    settings = Settings(_env_file=None)
    strong_seo, strong_uxui = _facts_for_fixture("strong_site.html")
    weak_seo, weak_uxui = _facts_for_fixture("weak_site.html")

    strong = score_audit(strong_seo, strong_uxui, _psi(92, 96), settings)
    weak = score_audit(weak_seo, weak_uxui, _psi(35, 50), settings)

    assert strong["scores"]["seo"] >= 85
    assert strong["scores"]["uxui"] >= 85
    assert strong["scores"]["lead_gen"] >= 85
    assert weak["scores"]["seo"] <= 35
    assert weak["scores"]["uxui"] <= 30
    assert weak["scores"]["lead_gen"] <= 35


def test_scoring_is_reproducible_for_same_facts() -> None:
    settings = Settings(_env_file=None)
    seo, uxui = _facts_for_fixture("strong_site.html")

    first = score_audit(seo, uxui, _psi(), settings)
    second = score_audit(seo, uxui, _psi(), settings)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # The CrUX CWV rules (7+6+5) and the 2 technical-crawl rules added in P2-16 (canonicals 5 +
    # redirect_chains 5) all skip_if_missing when the fixture carries no field/external-crawl data.
    # The P2-13 AEO rules are NOT skip_if_missing (they read on-page facts always present), so they
    # are scored here. The P2-15 a11y unique_referenced_ids rule (weight 2) skips on this fixture
    # because it uses no id-referencing label/ARIA attributes, so the skipped total is 106 + 2.
    assert first["categories"]["seo"]["weights"]["skipped"] == 108
    assert first["categories"]["seo"]["score"] >= 85


def test_scoring_does_not_treat_failed_technical_crawl_zero_summary_as_clean() -> None:
    settings = Settings(_env_file=None)
    seo, uxui = _facts_for_fixture("strong_site.html")
    legacy_failed_external = {
        "technical_crawl": {
            "status": "failed",
            "summary": _technical_crawl_zero_summary(),
            "issues": [],
        },
        "gsc": {"status": "skipped", "summary": {}},
        "url_inspection": {"status": "skipped", "summary": {}},
    }

    scored = score_audit(
        seo,
        uxui,
        _psi(),
        settings,
        external_seo_facts=legacy_failed_external,
    )

    rules = {
        rule["rule_id"]: rule
        for rule in scored["categories"]["seo"]["rules"]
        if rule["rule_id"].startswith("seo.technical_crawl.")
    }
    assert rules["seo.technical_crawl.no_broken_internal_urls"]["result"] == "skipped"
    assert rules["seo.technical_crawl.missing_titles"]["result"] == "skipped"
    assert rules["seo.technical_crawl.missing_meta_descriptions"]["result"] == "skipped"


def test_scoring_uses_complete_technical_crawl_zero_summary() -> None:
    settings = Settings(_env_file=None)
    seo, uxui = _facts_for_fixture("strong_site.html")
    complete_external = {
        "technical_crawl": {
            "status": "complete",
            "summary": _technical_crawl_zero_summary(),
            "issues": [],
        },
        "gsc": {"status": "skipped", "summary": {}},
        "url_inspection": {"status": "skipped", "summary": {}},
    }

    scored = score_audit(
        seo,
        uxui,
        _psi(),
        settings,
        external_seo_facts=complete_external,
    )

    rules = {
        rule["rule_id"]: rule
        for rule in scored["categories"]["seo"]["rules"]
        if rule["rule_id"].startswith("seo.technical_crawl.")
    }
    assert rules["seo.technical_crawl.no_broken_internal_urls"]["result"] == "pass"
    assert rules["seo.technical_crawl.missing_titles"]["result"] == "pass"
    assert rules["seo.technical_crawl.missing_meta_descriptions"]["result"] == "pass"


def _technical_crawl_zero_summary() -> dict:
    return {
        "urls_crawled": 0,
        "html_urls_crawled": 0,
        "client_error_internal_urls": 0,
        "server_error_internal_urls": 0,
        "client_error_external_urls": 0,
        "server_error_external_urls": 0,
        "non_indexable_internal_urls": 0,
        "missing_titles": 0,
        "duplicate_titles": 0,
        "missing_meta_descriptions": 0,
        "duplicate_meta_descriptions": 0,
        "missing_h1": 0,
        "images_missing_alt": 0,
        "missing_canonicals": 0,
    }
