import asyncio
import json

from apps.shared.config import Settings
from apps.worker.stages.accessibility import (
    DISCLAIMER_TEXT,
    normalize_accessibility_facts,
    run_axe_on_page,
)

# Raw axe-core-shaped result: one net-new render-dependent rule (color-contrast), one rule already
# covered by the SCORED rubric (image-alt -> must be de-duped out), one net-new critical rule.
RAW = {
    "violations": [
        {
            "id": "color-contrast",
            "impact": "serious",
            "help": "Elements must meet minimum colour contrast ratio thresholds",
            "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/color-contrast",
            "tags": ["wcag2aa", "wcag143"],
            "nodes": [
                {"target": [".hero .tagline"], "failureSummary": "Fix any: contrast 2.1:1"},
                {"target": [".footer a"]},
            ],
        },
        {
            "id": "image-alt",  # already scored by seo.images.alt_coverage -> de-duped
            "impact": "critical",
            "tags": ["wcag2a", "wcag111"],
            "nodes": [{"target": ["img.logo"]}],
        },
        {
            "id": "aria-required-attr",
            "impact": "critical",
            "help": "Required ARIA attributes must be provided",
            "helpUrl": "https://example.org/aria",
            "tags": ["wcag2a", "wcag412"],
            "nodes": [{"target": ['[role="checkbox"]']}],
        },
    ],
    "incomplete": [{"id": "color-contrast", "nodes": [{"target": [".maybe"]}]}],
}


def test_normalize_is_deterministic_and_dedupes_static_rules() -> None:
    per_page = [{"url": "https://x/", "result": RAW}, {"url": "https://x/about", "result": RAW}]
    first = normalize_accessibility_facts(per_page, axe_version="4.10.2")
    second = normalize_accessibility_facts(per_page, axe_version="4.10.2")

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    rule_ids = [issue["rule_id"] for issue in first["issues"]]
    assert "image-alt" not in rule_ids  # scored elsewhere -> ceded to the scored section
    assert {"color-contrast", "aria-required-attr"} == set(rule_ids)
    assert rule_ids[0] == "aria-required-attr"  # critical sorts before serious

    assert first["status"] == "complete"
    assert first["pages_scanned"] == 2
    assert first["axe_version"] == "4.10.2"
    assert first["needs_review_count"] == 2  # one incomplete node per page
    assert first["impact_counts"] == {"critical": 1, "serious": 1, "moderate": 0, "minor": 0}
    assert first["disclaimer"] == DISCLAIMER_TEXT
    assert first["notes"]  # cession note present when issues exist

    contrast = next(issue for issue in first["issues"] if issue["rule_id"] == "color-contrast")
    assert contrast["instances"] == 4  # 2 nodes x 2 pages
    assert contrast["example_pages"] == ["https://x/", "https://x/about"]
    assert contrast["wcag_criteria"] == ["wcag143", "wcag2aa"]


def test_normalize_skipped_when_no_results() -> None:
    assert normalize_accessibility_facts([])["status"] == "skipped"
    assert normalize_accessibility_facts([{"url": "x", "result": None}])["status"] == "skipped"


def test_normalize_caps_example_selectors() -> None:
    nodes = [{"target": [f".n{index}"]} for index in range(20)]
    raw = {
        "violations": [{"id": "aria-roles", "impact": "minor", "tags": ["wcag2a"], "nodes": nodes}],
        "incomplete": [],
    }
    out = normalize_accessibility_facts([{"url": "u", "result": raw}], max_examples=5)
    issue = out["issues"][0]
    assert issue["instances"] == 20
    assert len(issue["example_selectors"]) == 5


class _FakePage:
    """Minimal stand-in for a Playwright Page for the graceful-skip tests."""

    def __init__(self, *, evaluate_raises: bool = False) -> None:
        self._evaluate_raises = evaluate_raises

    async def evaluate(self, script: str, *args: object) -> object:
        if self._evaluate_raises:
            raise RuntimeError("evaluate boom")
        return {"violations": [], "incomplete": []}

    async def add_script_tag(self, path: str | None = None) -> None:
        return None


def test_run_axe_returns_none_when_asset_missing(tmp_path) -> None:
    settings = Settings(_env_file=None, accessibility_axe_script_path=tmp_path / "missing.js")
    assert asyncio.run(run_axe_on_page(_FakePage(), settings)) is None


def test_run_axe_returns_none_on_evaluate_error(tmp_path) -> None:
    asset = tmp_path / "axe.min.js"
    asset.write_text("// fake axe", encoding="utf-8")
    settings = Settings(_env_file=None, accessibility_axe_script_path=asset)
    assert asyncio.run(run_axe_on_page(_FakePage(evaluate_raises=True), settings)) is None


def test_run_axe_returns_result_on_success(tmp_path) -> None:
    asset = tmp_path / "axe.min.js"
    asset.write_text("// fake axe", encoding="utf-8")
    settings = Settings(_env_file=None, accessibility_axe_script_path=asset)
    result = asyncio.run(run_axe_on_page(_FakePage(), settings))
    assert result == {"violations": [], "incomplete": []}
