"""Optional advisory accessibility pass (P2-15b) — axe-core against the live browser DOM.

This is a DELIBERATELY isolated, advisory-only layer. It runs only when
``accessibility_advisory_enabled`` is set, executes axe-core inside the crawler's live page
(before the page closes), and produces FINDINGS ONLY. Its output is stored in a separate
``audit_results.accessibility_facts`` column and rendered as an advisory report section — it is
NEVER passed to ``scoring.score_audit`` (which reads only seo/uxui/psi/external_seo), so the
deterministic scores stay byte-for-byte reproducible whether this pass runs or not.

Why axe and not the static ``_extract_a11y`` rules: axe runs in a real rendered browser, so it
sees computed CSS and the accessibility tree — it adds the render-dependent checks a static HTML
parse cannot do (colour contrast, computed ARIA, visibility). The structural checks axe also does
are already SCORED by the ``seo.a11y.*`` rubric rules, so this pass de-dupes against them
(``STATIC_RULE_AXE_IDS``) and surfaces only the net-new, render-dependent issues.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings

JsonDict = dict[str, Any]

# axe rule ids already covered by the SCORED rubric (seo.a11y.* + seo.images.alt_coverage). The
# advisory pass cedes these to the scored sections (see CESSION_NOTE) so the same finding never
# appears twice, and keeps only axe's net-new, render-dependent checks (colour contrast, ARIA
# validity, landmark/region structure, heading order, tables, etc.).
STATIC_RULE_AXE_IDS = frozenset(
    {
        "html-has-lang",
        "html-lang-valid",
        "valid-lang",
        "label",
        "select-name",
        "aria-input-field-name",
        "link-name",
        "button-name",
        "image-alt",
        "duplicate-id-aria",
        "meta-viewport",
        "tabindex",
        "landmark-one-main",
    }
)

# critical -> serious -> moderate -> minor (axe's user-impact ordering, not WCAG level).
_IMPACT_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}

DISCLAIMER_TEXT = (
    "This is an automated accessibility scan (axe-core), provided as advisory guidance rather "
    "than a compliance verdict. Automated tools reliably detect only a portion of accessibility "
    "barriers — roughly a third to a half of issues, and only about a quarter of WCAG success "
    "criteria can be machine-tested. A clean result does not mean the site is accessible or "
    "legally compliant: many barriers (keyboard use, alt-text quality, reading order, the "
    "screen-reader experience) require manual review by a person. This is not a legal opinion "
    "and does not guarantee conformance with the ADA, WCAG, Section 508, the European "
    "Accessibility Act, or any other standard. Unlike the scored sections of this report, these "
    "findings are not reproducible run-to-run."
)

CESSION_NOTE = (
    "Document language, form labels, link and button names, image alt text, viewport zoom, and "
    "duplicated reference IDs are evaluated in the scored SEO accessibility checks above; the "
    "items here cover additional, render-dependent accessibility issues."
)


def _axe_options(settings: Settings) -> JsonDict:
    """Pinned, deterministic-as-possible axe run options: WCAG 2.x A+AA tags only (no
    best-practice/experimental churn), violations + needs-review only."""
    rules: JsonDict = {}
    if not settings.accessibility_run_contrast:
        rules["color-contrast"] = {"enabled": False}
    return {
        "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
        "resultTypes": ["violations", "incomplete"],
        "rules": rules,
    }


def read_axe_version(script_path: Path) -> str:
    sidecar = script_path.parent / "AXE_VERSION"
    with suppress(OSError):
        text = sidecar.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "unknown"


async def run_axe_on_page(page: Any, settings: Settings) -> JsonDict | None:
    """Inject the vendored axe-core and run it against the live DOM. Returns the raw axe result
    dict, or ``None`` on ANY failure (missing asset / evaluate error / timeout) so the
    accessibility pass can never raise into — or slow past a bound — the crawl. Celery's soft
    time limit is the one exception: it must propagate (crawler convention) or the honest
    timed-out failure path never runs and the hard limit SIGKILLs the worker mid-render."""
    script_path = Path(settings.accessibility_axe_script_path)
    try:
        if not script_path.is_file():
            return None
        try:
            # Let web fonts settle so contrast/geometry checks are as stable as possible.
            await page.evaluate("() => document.fonts && document.fonts.ready")
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            pass
        await page.add_script_tag(path=str(script_path))
        return await asyncio.wait_for(
            page.evaluate("(opts) => axe.run(document, opts)", _axe_options(settings)),
            timeout=settings.accessibility_axe_timeout_seconds,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _node_target(node: JsonDict) -> str:
    target = node.get("target")
    if isinstance(target, list):
        return " ".join(str(part) for part in target if part)
    return str(target) if target else ""


def _first_failure_summary(nodes: list[JsonDict]) -> str:
    for node in nodes:
        summary = node.get("failureSummary")
        if isinstance(summary, str) and summary.strip():
            return " ".join(summary.split())
    return ""


def _violation_impact(violation: JsonDict, nodes: list[JsonDict]) -> str:
    impact = violation.get("impact")
    if isinstance(impact, str) and impact in _IMPACT_ORDER:
        return impact
    best = "minor"
    for node in nodes:
        node_impact = node.get("impact")
        if (
            isinstance(node_impact, str)
            and node_impact in _IMPACT_ORDER
            and _IMPACT_ORDER[node_impact] < _IMPACT_ORDER[best]
        ):
            best = node_impact
    return best


def normalize_accessibility_facts(
    per_page: list[JsonDict],
    *,
    max_examples: int = 5,
    axe_version: str = "unknown",
) -> JsonDict:
    """Pure, deterministic aggregation of raw per-page axe results into the advisory bundle.

    ``per_page`` items are ``{"url": <page url>, "result": <raw axe result dict>}``. Same input
    always yields the same bundle (issues grouped by rule, capped, impact-sorted, static-rule
    duplicates dropped). Returns ``{"status": "skipped"}`` when no page produced a result.
    """
    scanned = [item for item in per_page if isinstance(item.get("result"), dict)]
    if not scanned:
        return {"status": "skipped"}

    issues: dict[str, JsonDict] = {}
    needs_review = 0
    for item in scanned:
        url = str(item.get("url") or "")
        result = item["result"]
        for incomplete in _list(result.get("incomplete")):
            needs_review += len(_list(incomplete.get("nodes")))
        for violation in _list(result.get("violations")):
            rule_id = str(violation.get("id") or "")
            if not rule_id or rule_id in STATIC_RULE_AXE_IDS:
                continue
            nodes = _list(violation.get("nodes"))
            if not nodes:
                continue
            entry = issues.get(rule_id)
            if entry is None:
                tags = [str(tag) for tag in _list(violation.get("tags"))]
                entry = {
                    "rule_id": rule_id,
                    "impact": _violation_impact(violation, nodes),
                    "wcag_criteria": sorted({tag for tag in tags if tag.startswith("wcag")}),
                    "help": str(violation.get("help") or ""),
                    "help_url": str(violation.get("helpUrl") or ""),
                    "instances": 0,
                    "selectors": [],
                    "pages": [],
                    "failure_summary": _first_failure_summary(nodes),
                }
                issues[rule_id] = entry
            entry["instances"] += len(nodes)
            for node in nodes:
                selector = _node_target(node)
                if selector and selector not in entry["selectors"]:
                    entry["selectors"].append(selector)
            if url and url not in entry["pages"]:
                entry["pages"].append(url)

    normalized = [
        {
            "rule_id": entry["rule_id"],
            "impact": entry["impact"],
            "wcag_criteria": entry["wcag_criteria"],
            "help": entry["help"],
            "help_url": entry["help_url"],
            "instances": entry["instances"],
            "example_selectors": entry["selectors"][:max_examples],
            "example_pages": entry["pages"][:3],
            "failure_summary": entry["failure_summary"],
        }
        for entry in issues.values()
    ]
    normalized.sort(
        key=lambda issue: (
            _IMPACT_ORDER.get(issue["impact"], 9),
            -issue["instances"],
            issue["rule_id"],
        )
    )

    impact_counts = {level: 0 for level in ("critical", "serious", "moderate", "minor")}
    for issue in normalized:
        if issue["impact"] in impact_counts:
            impact_counts[issue["impact"]] += 1

    return {
        "status": "complete",
        "axe_version": axe_version,
        "pages_scanned": len(scanned),
        "impact_counts": impact_counts,
        "needs_review_count": needs_review,
        "issues": normalized,
        "disclaimer": DISCLAIMER_TEXT,
        "notes": [CESSION_NOTE] if normalized else [],
    }
