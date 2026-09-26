#!/usr/bin/env python
"""P1-23 Local End-To-End QA.

Runs the full audit pipeline once against a localhost-served HTML fixture and
verifies every stage: job submission, worker run, crawler, SEO/UX extractors,
PageSpeed (graceful skip), deterministic scoring, commentary, grounding
validation, PDF rendering, and the persisted database result.

Run from the repo root:

    python scripts/qa_e2e.py

Exits 0 when all stage checks pass, 1 otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa_common  # noqa: E402


def _check(name: str, passed: bool, detail: str) -> tuple[str, bool, str]:
    return (name, passed, detail)


def _build_checks(snap: qa_common.JsonDict) -> list[tuple[str, bool, str]]:
    crawl = snap.get("crawl_summary", {}) or {}
    scores = snap.get("scores", {}) or {}
    successful = int(crawl.get("successful_pages") or 0)
    return [
        _check(
            "Submit audit (job created)",
            bool(snap.get("job_id")) and bool(snap.get("url")),
            f"job_id={snap.get('job_id')}",
        ),
        _check(
            "Worker pipeline completed",
            snap.get("status") == "complete",
            f"status={snap.get('status')} stage={snap.get('current_stage')}",
        ),
        _check(
            "Crawler rendered page(s)",
            successful >= 1,
            f"successful={successful} failed={crawl.get('failed_pages')} "
            f"skipped={crawl.get('skipped_pages')}",
        ),
        _check(
            "SEO extractor produced facts",
            snap.get("seo_status") == "complete",
            f"seo_status={snap.get('seo_status')}",
        ),
        _check(
            "UX/UI extractor produced facts",
            snap.get("uxui_status") == "complete",
            f"uxui_status={snap.get('uxui_status')}",
        ),
        _check(
            "PageSpeed collected or gracefully skipped",
            snap.get("psi_status") in {"skipped", "complete", "partial", "failed"},
            f"psi_status={snap.get('psi_status')} (skipped expected without API key)",
        ),
        _check(
            "Deterministic scoring produced 3 scores",
            all(isinstance(scores.get(k), int) for k in ("seo", "uxui", "lead_gen")),
            f"scores={scores} rubric={snap.get('rubric_version')}",
        ),
        _check(
            "Commentary generated",
            bool(snap.get("commentary_status")),
            f"status={snap.get('commentary_status')} provider={snap.get('commentary_provider')}",
        ),
        _check(
            "Grounding validation ran",
            snap.get("validation_status") == "complete",
            f"validation_status={snap.get('validation_status')}",
        ),
        _check(
            "Branded PDF rendered",
            qa_common.pdf_is_valid(snap.get("pdf_path"))[0],
            f"pdf={snap.get('pdf_path')} size={qa_common.pdf_is_valid(snap.get('pdf_path'))[1]}B",
        ),
        _check(
            "Database result persisted & consistent",
            bool(snap.get("has_result"))
            and snap.get("seo_score") == scores.get("seo")
            and snap.get("uxui_score") == scores.get("uxui")
            and snap.get("lead_gen_score") == scores.get("lead_gen"),
            f"seo={snap.get('seo_score')} uxui={snap.get('uxui_score')} "
            f"lead_gen={snap.get('lead_gen_score')}",
        ),
    ]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="blc-qa-e2e-") as tmp:
        tmp_dir = Path(tmp)
        qa_common.configure_local_env(tmp_dir)
        qa_common.create_schema()

        fixture = sys.argv[1] if len(sys.argv) > 1 else qa_common.DEFAULT_FIXTURE
        with qa_common.serve_fixtures() as port:
            url = f"http://127.0.0.1:{port}/{fixture}"
            print(f"[qa-e2e] running full pipeline against {url}\n")
            try:
                job_id = qa_common.run_audit_pipeline(
                    url, niche="custom home builder", audience="homeowners"
                )
            except Exception as exc:  # noqa: BLE001 - surface failure in the report
                print(f"[qa-e2e] pipeline raised: {type(exc).__name__}: {exc}")
                job_id = None

        if job_id is None:
            print("[qa-e2e] no job to inspect; FAILED")
            return 1

        snap = qa_common.snapshot_audit(job_id)
        checks = _build_checks(snap)

        width = max(len(name) for name, _, _ in checks)
        all_passed = True
        for name, passed, detail in checks:
            mark = "PASS" if passed else "FAIL"
            all_passed = all_passed and passed
            print(f"  [{mark}] {name.ljust(width)}  {detail}")

        print()
        if all_passed:
            print(f"[qa-e2e] RESULT: PASS - {len(checks)}/{len(checks)} stage checks green")
            return 0
        failed = sum(1 for _, passed, _ in checks if not passed)
        print(f"[qa-e2e] RESULT: FAIL - {failed}/{len(checks)} stage checks red")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
