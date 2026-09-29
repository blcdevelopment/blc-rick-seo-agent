"""Unit coverage for the pure helpers in the QA harness (scripts/qa_common.py).

The full pipeline runs live in scripts/qa_e2e.py and scripts/qa_reproducibility.py;
these tests just protect the deterministic helper logic those scripts rely on.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import qa_common  # noqa: E402


def test_pdf_is_valid_detects_real_pdf(tmp_path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\nbody")
    ok, size = qa_common.pdf_is_valid(str(pdf))
    assert ok is True
    assert size == len(b"%PDF-1.7\nbody")


def test_pdf_is_valid_rejects_non_pdf_and_missing(tmp_path) -> None:
    not_pdf = tmp_path / "report.pdf"
    not_pdf.write_bytes(b"<html></html>")
    assert qa_common.pdf_is_valid(str(not_pdf)) == (False, len(b"<html></html>"))
    assert qa_common.pdf_is_valid(str(tmp_path / "missing.pdf")) == (False, 0)
    assert qa_common.pdf_is_valid(None) == (False, 0)


def test_rule_results_flattens_category_breakdown() -> None:
    breakdown = {
        "categories": {
            "seo": {
                "rules": [
                    {"rule_id": "seo.title", "result": "pass"},
                    {"rule_id": "seo.meta", "result": "fail"},
                ]
            }
        }
    }
    assert qa_common.rule_results(breakdown, "seo") == {
        "seo.title": "pass",
        "seo.meta": "fail",
    }
    assert qa_common.rule_results(breakdown, "uxui") == {}
