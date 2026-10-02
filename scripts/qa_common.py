"""Shared harness for Phase 1 QA scripts (P1-23, P1-24).

This runs the *real* audit pipeline end-to-end with zero external dependencies:

- A localhost HTTP server serves the bundled HTML fixtures, so the real
  Playwright crawler renders a real page (no internet required).
- The database is an ephemeral SQLite file (no PostgreSQL required).
- PageSpeed Insights runs its real graceful-skip path (no API key required).
- Commentary is fully deterministic by construction in Phase 1 (no OpenAI call
  is made at all), so no API key is involved.

Everything else - crawler, SEO/UX extractors, deterministic scoring, grounding
validation, report payload, and WeasyPrint PDF rendering - is the production
code path. Because no live network/LLM call is made, runs are deterministic,
which is exactly what the reproducibility QA needs.
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
DEFAULT_FIXTURE = "strong_site.html"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

JsonDict = dict[str, Any]


def configure_local_env(tmp_dir: Path) -> None:
    """Point the app at an ephemeral, network-free, key-free environment.

    Must run before any ``apps.*`` import so the settings cache and the database
    engine are built against these values.
    """
    db_path = tmp_dir / "qa.sqlite"
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "OPENAI_API_KEY": "",
            "GOOGLE_PSI_API_KEY": "",
            "AUDIT_ENQUEUE_ENABLED": "false",
            "LOCAL_REPORT_STORAGE_DIR": str(tmp_dir / "reports"),
            "LOCAL_SCREENSHOT_STORAGE_DIR": str(tmp_dir / "screenshots"),
            "CRAWLER_ALLOW_PRIVATE_HOSTS": "true",
            "CRAWLER_RESPECT_ROBOTS_TXT": "false",
            "CRAWLER_SCREENSHOTS_ENABLED": "true",
            # Pin the external SEO stage to its real skip paths regardless of the
            # operator's gitignored .env: no Screaming Frog subprocess, no site
            # health link sweep (fixture root-relative links would 404 against the
            # fixture server and change scores), no Google OAuth.
            "SCREAMING_FROG_ENABLED": "false",
            "SITE_HEALTH_ENABLED": "false",
            "GOOGLE_OAUTH_CLIENT_ID": "",
            "GOOGLE_OAUTH_CLIENT_SECRET": "",
            # AI Visibility auto-runs on every audit when enabled; force it off so the hermetic
            # harness never launches a Semrush login-bot / OpenAI vision call (keeps QA offline
            # and byte-identical regardless of the operator's .env).
            "AI_VISIBILITY_ENABLED": "false",
            # Social + enrichment providers are paid/quota'd: a fixture that ever links a social
            # profile must not trigger real Apify / YouTube / Places calls from the .env keys.
            "SOCIAL_AUTODISCOVERY_ENABLED": "false",
            "APIFY_API_TOKEN": "",
            "YOUTUBE_API_KEY": "",
            "GOOGLE_PLACES_API_KEY": "",
            # Firecrawl (the blocked-site fallback) is billed per page: never from a QA run.
            "FIRECRAWL_API_KEY": "",
            "BENCHMARK_ENABLED": "false",
            # No outbound error reports from a local QA run.
            "SENTRY_DSN": "",
        }
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # noqa: D401 - silence access log
        return


@contextmanager
def serve_fixtures(directory: Path = FIXTURES_DIR) -> Iterator[int]:
    """Serve ``directory`` over localhost; yields the bound port."""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def create_schema() -> None:
    from apps.shared.database import engine
    from apps.shared.models import Base

    Base.metadata.create_all(engine)


def run_audit_pipeline(url: str, *, niche: str | None = None, audience: str | None = None) -> str:
    """Create an audit job and run the real collection pipeline. Returns job id."""
    from apps.shared.database import SessionLocal
    from apps.shared.models import AuditJob
    from apps.worker.tasks import run_collection_audit

    with SessionLocal() as db:
        job = AuditJob(
            url=url,
            niche=niche,
            target_audience=audience,
            status="queued",
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = str(job.id)

    # Uses the default real crawler + real PSI collector (graceful skip w/o key).
    run_collection_audit(job_id)
    return job_id


def snapshot_audit(job_id: str) -> JsonDict:
    """Read the persisted job + result back into a plain dict for verification."""
    from uuid import UUID

    from apps.shared.database import SessionLocal
    from apps.shared.models import AuditJob

    with SessionLocal() as db:
        job = db.get(AuditJob, UUID(job_id))
        if job is None:
            raise RuntimeError(f"Audit job {job_id} disappeared")
        result = job.result
        snap: JsonDict = {
            "job_id": job_id,
            "url": job.url,
            "status": job.status,
            "current_stage": job.current_stage,
            "progress_pct": job.progress_pct,
            "error_message": job.error_message,
            "has_result": result is not None,
        }
        if result is not None:
            crawl_summary = (result.crawled_pages or {}).get("summary", {})
            snap.update(
                {
                    "crawl_summary": crawl_summary,
                    "seo_status": (result.seo_facts or {}).get("status"),
                    "uxui_status": (result.uxui_facts or {}).get("status"),
                    "psi_status": (result.psi_facts or {}).get("status"),
                    "scores": (result.score_breakdown or {}).get("scores", {}),
                    "rubric_version": result.rubric_version,
                    "seo_score": result.seo_score,
                    "uxui_score": result.uxui_score,
                    "lead_gen_score": result.lead_gen_score,
                    "commentary": result.commentary,
                    "commentary_status": (result.commentary or {}).get("status"),
                    "commentary_provider": (result.commentary or {}).get("provider"),
                    "llm_model": result.llm_model,
                    "validation_status": (result.validation_log or {}).get("status"),
                    "validation_log": result.validation_log,
                    "score_breakdown": result.score_breakdown,
                    "pdf_path": result.pdf_path,
                    "report_metadata": result.report_metadata,
                }
            )
        return snap


def rule_results(score_breakdown: JsonDict, category: str) -> dict[str, str]:
    """Flatten a category's rule breakdown into {rule_id: result}."""
    rules = (score_breakdown.get("categories", {}).get(category, {}) or {}).get("rules", [])
    return {rule["rule_id"]: rule["result"] for rule in rules}


def pdf_is_valid(pdf_path: str | None) -> tuple[bool, int]:
    """Return (is_valid_pdf, size_bytes)."""
    if not pdf_path:
        return False, 0
    path = Path(pdf_path)
    if not path.exists():
        return False, 0
    size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(5)
    return header == b"%PDF-" and size > 0, size
