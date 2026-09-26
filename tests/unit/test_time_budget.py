"""Time-budget guards that keep a real audit inside the Celery soft time limit.

PageSpeed runs serially per page and Screaming Frog can crawl a whole site, so each
expensive stage must respect a budget and degrade gracefully (partial/skip) instead of
letting the task overrun and fail with SoftTimeLimitExceeded.
"""

import time

from apps.shared.config import Settings
from apps.worker.stages import external_seo, psi_client
from apps.worker.stages import screaming_frog as sf
from apps.worker.tasks import _PIPELINE_TAIL_RESERVE_SECONDS, _external_enrichment_deadline


def test_psi_stops_at_total_budget(monkeypatch) -> None:
    fetched: list[str] = []

    def fake_fetch(url: str, strategy: str, settings: Settings) -> dict:
        fetched.append(url)
        return {"status": "complete", "strategy": strategy, "scores": {"performance": 90}}

    settings = Settings(
        _env_file=None,
        google_psi_api_key="psi-secret",
        psi_scope="all_crawled_pages",
        psi_max_pages=5,
        psi_total_budget_seconds=30,
    )

    monkeypatch.setattr(psi_client, "_fetch_strategy", fake_fetch)
    # Fake monotonic clock: returns 0 for the deadline calc and the first URL's budget
    # check, then jumps past the 30s budget so the loop stops before the second URL.
    calls = {"n": 0}

    def fake_monotonic() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 1000.0

    monkeypatch.setattr(psi_client.time, "monotonic", fake_monotonic)

    facts = psi_client.collect_pagespeed_facts(
        ["https://a.example/", "https://b.example/", "https://c.example/"],
        settings,
    )

    assert facts["status"] == "partial"
    assert facts["pages_analyzed"] == 1
    assert facts["pages_requested"] == 3
    assert set(fetched) == {"https://a.example/"}


def test_screaming_frog_timeout_shrinks_under_deadline() -> None:
    settings = Settings(
        _env_file=None,
        screaming_frog_timeout_seconds=1800,
        celery_task_soft_time_limit_seconds=840,
        celery_task_time_limit_seconds=900,
    )
    # No deadline: clamped to the soft limit minus the 120s tail reserve.
    assert sf._effective_timeout_seconds(settings) == 720
    # A deadline only seconds out leaves no usable crawl time.
    assert (
        sf._effective_timeout_seconds(settings, deadline=time.monotonic() + 5) < sf._MIN_RUN_SECONDS
    )


def test_screaming_frog_skips_when_out_of_time(monkeypatch) -> None:
    monkeypatch.setattr(sf, "_resolve_binary", lambda configured: "/usr/bin/true")
    settings = Settings(
        _env_file=None,
        screaming_frog_enabled=True,
        screaming_frog_timeout_seconds=1800,
        celery_task_soft_time_limit_seconds=840,
        celery_task_time_limit_seconds=900,
    )
    facts = sf.collect_screaming_frog_facts(
        "https://example.com/", "audit-1", settings, deadline=time.monotonic() - 10
    )
    assert facts["status"] == "skipped"
    assert facts["reason"] == "insufficient_time_budget"


def test_external_seo_skips_gsc_when_out_of_time() -> None:
    settings = Settings(
        _env_file=None,
        screaming_frog_enabled=False,
        site_health_enabled=False,
    )
    facts = external_seo.collect_external_seo_facts(
        url="https://example.com/",
        audit_id="audit-1",
        page_urls=["https://example.com/"],
        settings=settings,
        db=None,
        deadline=time.monotonic() - 1,
    )
    assert facts["gsc"]["status"] == "skipped"
    assert facts["gsc"]["reason"] == "insufficient_time_budget"
    assert facts["url_inspection"]["status"] == "skipped"


def test_external_enrichment_deadline_reserves_tail() -> None:
    settings = Settings(
        _env_file=None,
        celery_task_soft_time_limit_seconds=840,
        celery_task_time_limit_seconds=900,
    )
    deadline = _external_enrichment_deadline(settings, 1000.0)
    assert deadline == 1000.0 + (840 - _PIPELINE_TAIL_RESERVE_SECONDS)
