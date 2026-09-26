from apps.shared.config import Settings
from apps.worker.stages import psi_client
from apps.worker.stages.psi_client import collect_pagespeed_facts, normalize_pagespeed_response


def test_normalize_pagespeed_response_extracts_scores_and_lab_metrics() -> None:
    payload = {
        "id": "https://example.com/",
        "lighthouseResult": {
            "finalDisplayedUrl": "https://example.com/",
            "fetchTime": "2026-05-29T10:00:00Z",
            "categories": {
                "performance": {"score": 0.91},
                "accessibility": {"score": 0.88},
                "best-practices": {"score": 0.97},
                "seo": {"score": 1},
            },
            "audits": {
                "first-contentful-paint": {"numericValue": 1200.4},
                "largest-contentful-paint": {"numericValue": 2100},
                "speed-index": {"numericValue": 1800},
                "total-blocking-time": {"numericValue": 45},
                "cumulative-layout-shift": {"numericValue": 0.02},
                "modern-image-formats": {"score": 0.5},
                "document-title": {"score": 1},
            },
        },
    }

    facts = normalize_pagespeed_response(payload, "mobile")

    assert facts["status"] == "complete"
    assert facts["scores"] == {
        "performance": 91,
        "accessibility": 88,
        "best_practices": 97,
        "seo": 100,
    }
    assert facts["lab_metrics"]["largest_contentful_paint_ms"] == 2100.0
    assert facts["lab_metrics"]["cumulative_layout_shift"] == 0.02
    assert facts["audit_scores"]["modern_image_formats"] == 50
    assert facts["audit_scores"]["document_title"] == 100


def test_collect_pagespeed_facts_skips_without_api_key() -> None:
    settings = Settings(_env_file=None, google_psi_api_key=None)

    facts = collect_pagespeed_facts("https://example.com/", settings)

    assert facts["status"] == "skipped"
    assert facts["reason"] == "missing_google_psi_api_key"
    assert facts["scope"] == "all_crawled_pages"
    assert facts["pages_analyzed"] == 0
    assert facts["pages"] == []
    assert facts["strategies"] == {}


def test_collect_pagespeed_facts_sends_api_key_in_header(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "id": "https://example.com/",
                "lighthouseResult": {
                    "finalDisplayedUrl": "https://example.com/",
                    "categories": {},
                    "audits": {},
                },
            }

    class FakeClient:
        def __init__(self, timeout) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def get(self, url, params, headers):
            calls.append({"url": url, "params": params, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(psi_client.httpx, "Client", FakeClient)
    settings = Settings(
        _env_file=None,
        google_psi_api_key="psi-secret",
        psi_cache_ttl_seconds=0,
        psi_max_retries=1,
    )

    facts = collect_pagespeed_facts("https://example.com/", settings)

    assert facts["status"] == "complete"
    assert facts["pages_analyzed"] == 1
    assert facts["pages"][0]["url"] == "https://example.com/"
    assert len(calls) == 2
    for call in calls:
        assert call["headers"]["x-goog-api-key"] == "psi-secret"
        assert all(name != "key" for name, _ in call["params"])


def test_collect_pagespeed_facts_runs_all_crawled_pages_up_to_psi_max(monkeypatch) -> None:
    calls = []

    def fake_fetch(url: str, strategy: str, settings: Settings) -> dict:
        calls.append((url, strategy))
        performance_scores = {
            "https://example.com/": 95,
            "https://example.com/services": 70,
        }
        return {
            "status": "complete",
            "strategy": strategy,
            "scores": {
                "performance": performance_scores[url],
                "accessibility": None,
                "best_practices": None,
                "seo": None,
            },
        }

    monkeypatch.setattr(psi_client, "_fetch_strategy", fake_fetch)
    settings = Settings(
        _env_file=None,
        google_psi_api_key="psi-secret",
        psi_scope="all_crawled_pages",
        psi_max_pages=2,
        crawler_max_pages=5,
    )

    facts = collect_pagespeed_facts(
        [
            "https://example.com/",
            "https://example.com/services",
            "https://example.com/contact",
        ],
        settings,
    )

    assert facts["status"] == "complete"
    assert facts["scope"] == "all_crawled_pages"
    assert facts["max_pages"] == 2
    assert facts["pages_requested"] == 3
    assert facts["pages_analyzed"] == 2
    assert calls == [
        ("https://example.com/", "mobile"),
        ("https://example.com/", "desktop"),
        ("https://example.com/services", "mobile"),
        ("https://example.com/services", "desktop"),
    ]
    assert facts["summary"]["avg_mobile_performance"] == 83
    assert facts["summary"]["avg_desktop_performance"] == 83
    assert facts["summary"]["slowest_pages"][0]["url"] == "https://example.com/services"


def test_collect_pagespeed_facts_homepage_scope_only_runs_first_page(monkeypatch) -> None:
    calls = []

    def fake_fetch(url: str, strategy: str, settings: Settings) -> dict:
        calls.append((url, strategy))
        return {
            "status": "complete",
            "strategy": strategy,
            "scores": {"performance": 90},
        }

    monkeypatch.setattr(psi_client, "_fetch_strategy", fake_fetch)
    settings = Settings(
        _env_file=None,
        google_psi_api_key="psi-secret",
        psi_scope="homepage",
        psi_max_pages=10,
    )

    facts = collect_pagespeed_facts(
        ["https://example.com/", "https://example.com/contact"],
        settings,
    )

    assert facts["status"] == "complete"
    assert facts["scope"] == "homepage"
    assert facts["max_pages"] == 1
    assert facts["pages_analyzed"] == 1
    assert calls == [
        ("https://example.com/", "mobile"),
        ("https://example.com/", "desktop"),
    ]


def test_normalize_pagespeed_response_extracts_crux_field_data() -> None:
    payload = {
        "lighthouseResult": {"categories": {"performance": {"score": 0.86}}, "audits": {}},
        "loadingExperience": {
            "overall_category": "AVERAGE",
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3270, "category": "AVERAGE"},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 3, "category": "FAST"},
            },
        },
        "originLoadingExperience": {
            "overall_category": "SLOW",
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4200, "category": "SLOW"},
            },
        },
    }
    facts = normalize_pagespeed_response(payload, "mobile")

    page = facts["field_data"]["page"]
    origin = facts["field_data"]["origin"]
    assert page["overall_category"] == "AVERAGE"
    assert page["largest_contentful_paint_ms"] == {"p75": 3270, "category": "AVERAGE"}
    # PSI scales the field CLS by 100; we normalize 3 -> 0.03 so downstream is in real units.
    assert page["cumulative_layout_shift"] == {"p75": 0.03, "category": "FAST"}
    assert origin["overall_category"] == "SLOW"
    # Absent metrics (e.g. INP "missing data") stay None, never fabricated.
    assert origin["interaction_to_next_paint_ms"] is None


def test_normalize_pagespeed_response_without_field_data() -> None:
    facts = normalize_pagespeed_response({"lighthouseResult": {}}, "mobile")
    assert facts["field_data"] == {"page": None, "origin": None}


def test_summary_aggregates_origin_core_web_vitals(monkeypatch) -> None:
    def fake_fetch(url: str, strategy: str, settings: Settings) -> dict:
        return {
            "status": "complete",
            "strategy": strategy,
            "scores": {
                "performance": 80,
                "accessibility": None,
                "best_practices": None,
                "seo": None,
            },
            "field_data": {
                "page": None,
                "origin": {
                    "overall_category": "AVERAGE",
                    "largest_contentful_paint_ms": {"p75": 3200, "category": "AVERAGE"},
                    "interaction_to_next_paint_ms": {"p75": 180, "category": "FAST"},
                    "cumulative_layout_shift": {"p75": 0.04, "category": "FAST"},
                },
            },
        }

    monkeypatch.setattr(psi_client, "_fetch_strategy", fake_fetch)
    settings = Settings(_env_file=None, google_psi_api_key="psi-secret", psi_scope="homepage")
    crux = collect_pagespeed_facts("https://example.com/", settings)["summary"]["crux"]
    assert crux["has_field_data"] is True
    assert crux["lcp_p75_ms"] == 3200
    assert crux["inp_p75_ms"] == 180
    assert crux["cls_p75"] == 0.04
    assert crux["overall_category"] == "AVERAGE"


def test_summary_crux_is_none_without_field_data(monkeypatch) -> None:
    def fake_fetch(url: str, strategy: str, settings: Settings) -> dict:
        return {"status": "complete", "strategy": strategy, "scores": {"performance": 90}}

    monkeypatch.setattr(psi_client, "_fetch_strategy", fake_fetch)
    settings = Settings(_env_file=None, google_psi_api_key="psi-secret", psi_scope="homepage")
    crux = collect_pagespeed_facts("https://example.com/", settings)["summary"]["crux"]
    # No field data -> values stay None so the CWV rules skip_if_missing (never penalize).
    assert crux["has_field_data"] is False
    assert crux["lcp_p75_ms"] is None
    assert crux["inp_p75_ms"] is None
    assert crux["cls_p75"] is None
