"""Competitor benchmarking layer — provider registry, graceful skip, pure report builder,
and the deferred/byte-identical invariants (P2-26 / SMWA-79, Epic P2-E5 v3).

Benchmarking is a graceful-skip scaffold: OFF by default, no live vendor client yet, and it must
never change a score or render a section unless real baseline data is present.
"""

from apps.shared.config import Settings
from apps.worker.stages.benchmarking.collector import (
    collect_benchmark_facts,
    normalize_benchmark_facts,
)
from apps.worker.stages.benchmarking.providers import (
    BenchmarkProvider,
    get_provider,
    supported_providers,
)
from apps.worker.stages.benchmarking.report import build_benchmark_report_data


def _settings(**overrides) -> Settings:
    base = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


# --- registry -------------------------------------------------------------------------------


def test_registry_covers_supported_vendors() -> None:
    assert set(supported_providers()) == {"semrush", "ahrefs", "similarweb"}
    for name in supported_providers():
        provider = get_provider(name)
        assert provider is not None
        assert provider.name == name
        assert isinstance(provider, BenchmarkProvider)


def test_get_provider_unknown_vendor_is_none() -> None:
    assert get_provider("moz") is None
    assert get_provider("") is None


def test_credential_available_requires_selected_vendor_and_key() -> None:
    provider = get_provider("semrush")
    assert provider.credential_available(_settings()) is False
    # Key without selecting this vendor => still unavailable.
    assert provider.credential_available(_settings(benchmark_api_key="k")) is False
    # Wrong vendor selected => unavailable even with a key.
    assert (
        provider.credential_available(_settings(benchmark_provider="ahrefs", benchmark_api_key="k"))
        is False
    )
    # Selected + keyed => available.
    assert (
        provider.credential_available(
            _settings(benchmark_provider="semrush", benchmark_api_key="k")
        )
        is True
    )
    # Vendor key is normalized (case/whitespace/trailing newline from env values).
    assert (
        provider.credential_available(
            _settings(benchmark_provider="  Semrush\n", benchmark_api_key="k")
        )
        is True
    )


# --- collector graceful skip ---------------------------------------------------------------


def test_collect_skips_when_disabled_by_default() -> None:
    facts = collect_benchmark_facts(_settings(), target_url="https://acme.test")
    assert facts["status"] == "skipped"
    assert facts["reason"] == "benchmarking_disabled"
    assert facts["competitors"] == []


def test_collect_skips_without_provider() -> None:
    facts = collect_benchmark_facts(
        _settings(benchmark_enabled=True), target_url="https://acme.test"
    )
    assert facts["status"] == "skipped"
    assert facts["reason"] == "no_benchmark_provider_selected"


def test_collect_skips_without_api_key() -> None:
    facts = collect_benchmark_facts(
        _settings(benchmark_enabled=True, benchmark_provider="semrush"),
        target_url="https://acme.test",
    )
    assert facts["status"] == "skipped"
    assert facts["reason"] == "missing_benchmark_api_key"


def test_collect_skips_when_vendor_client_not_implemented() -> None:
    # Fully configured, but the paid vendor client is a deferred no-op today.
    facts = collect_benchmark_facts(
        _settings(benchmark_enabled=True, benchmark_provider="semrush", benchmark_api_key="k"),
        target_url="https://acme.test",
    )
    assert facts["status"] == "skipped"
    assert facts["reason"] == "provider_not_implemented"


def test_collect_normalizes_vendor_key_before_lookup() -> None:
    # A messy env value (case + whitespace + trailing newline) still resolves the vendor, so the
    # skip reason is the not-implemented stub, not "no provider selected".
    facts = collect_benchmark_facts(
        _settings(benchmark_enabled=True, benchmark_provider="  SEMRUSH\n", benchmark_api_key="k"),
        target_url="https://acme.test",
    )
    assert facts["reason"] == "provider_not_implemented"


# --- normalize -----------------------------------------------------------------------------


def test_normalize_builds_baselines_and_drops_junk() -> None:
    raw = {
        "competitors": [
            {"label": "rival.com", "seo": 70, "uxui": 55, "lead_gen": 62},
            {"label": "Industry", "is_industry": True, "overall": 68},
            {"label": "", "seo": 90},  # dropped: no label
            {"label": "empty.com"},  # dropped: no usable metric
            "not-a-dict",  # dropped: wrong type
        ]
    }
    facts = normalize_benchmark_facts(
        raw, provider="semrush", target_url="https://acme.test", niche="builders"
    )
    assert facts["status"] == "complete"
    labels = [c["label"] for c in facts["competitors"]]
    assert labels == ["rival.com", "Industry"]
    assert facts["competitors"][1]["is_industry"] is True


def test_normalize_empty_when_nothing_usable() -> None:
    facts = normalize_benchmark_facts(
        {"competitors": []}, provider="semrush", target_url="u", niche=None
    )
    assert facts["status"] == "empty"


def test_normalize_non_dict_payload_is_empty_not_raise() -> None:
    # A malformed provider payload must degrade to `empty`, not raise, even called directly.
    for bad in ("not-a-dict", ["rival.com"], 42, None):
        facts = normalize_benchmark_facts(bad, provider="semrush", target_url="u", niche=None)
        assert facts["status"] == "empty"
        assert facts["competitors"] == []


def test_normalize_non_finite_metric_is_dropped_not_raise() -> None:
    # A provider emitting NaN/inf must not raise inside int() — the bad metric is dropped, and a
    # sibling finite metric on the same row still lands.
    raw = {
        "competitors": [
            {"label": "rival.com", "seo": float("nan"), "uxui": float("inf"), "lead_gen": 65},
        ]
    }
    facts = normalize_benchmark_facts(raw, provider="semrush", target_url="u", niche=None)
    assert facts["status"] == "complete"
    row = facts["competitors"][0]
    assert row["seo"] is None and row["uxui"] is None and row["lead_gen"] == 65


def test_normalize_clamps_metric_to_0_100_int() -> None:
    raw = {"competitors": [{"label": "r", "seo": 130, "uxui": -5, "lead_gen": 62.6}]}
    row = normalize_benchmark_facts(raw, provider="p", target_url="u", niche=None)["competitors"][0]
    assert row["seo"] == 100
    assert row["uxui"] == 0
    assert row["lead_gen"] == 63  # half-up


# --- report builder ------------------------------------------------------------------------


def test_report_builder_returns_none_when_not_run() -> None:
    # No benchmark facts (the default path) => no section, byte-identical report.
    scores = {"seo": 80, "uxui": 70, "lead_gen": 75, "social": None, "overall": None}
    assert build_benchmark_report_data(scores=scores, benchmark_facts=None) is None


def test_report_builder_returns_none_for_skipped_facts() -> None:
    scores = {"seo": 80, "uxui": 70, "lead_gen": 75, "social": None, "overall": None}
    skipped = {"status": "skipped", "reason": "benchmarking_disabled", "competitors": []}
    assert build_benchmark_report_data(scores=scores, benchmark_facts=skipped) is None


def test_report_builder_computes_deltas_and_verdicts() -> None:
    scores = {"seo": 80, "uxui": 70, "lead_gen": 75, "social": None, "overall": None}
    facts = {
        "status": "complete",
        "provider": "semrush",
        "target_url": "https://acme.test",
        "competitors": [
            {"label": "rival.com", "seo": 70, "uxui": 70, "lead_gen": 82, "social": 40},
        ],
    }
    section = build_benchmark_report_data(scores=scores, benchmark_facts=facts)
    assert section is not None
    metrics = {m["metric"]: m for m in section["competitors"][0]["metrics"]}
    # social is skipped (audited score is None), so only the three present metrics compare.
    assert set(metrics) == {"seo", "uxui", "lead_gen"}
    assert metrics["seo"]["delta"] == 10 and metrics["seo"]["verdict"] == "ahead"
    assert metrics["uxui"]["delta"] == 0 and metrics["uxui"]["verdict"] == "on_par"
    assert metrics["lead_gen"]["delta"] == -7 and metrics["lead_gen"]["verdict"] == "behind"
    # Presentation strings are pre-formatted in the builder (single source of truth for both
    # renderers), so PDF and DOCX can't drift.
    assert metrics["seo"]["delta_display"] == "+10" and metrics["seo"]["verdict_label"] == "Ahead"
    assert metrics["uxui"]["delta_display"] == "0" and metrics["uxui"]["verdict_label"] == "On par"
    assert metrics["lead_gen"]["delta_display"] == "-7"
    assert metrics["lead_gen"]["verdict_label"] == "Behind"


def test_report_builder_none_when_no_metric_overlaps() -> None:
    # Baseline only has a metric the audit didn't score => nothing to compare => no section.
    scores = {"seo": 80, "uxui": 70, "lead_gen": 75, "social": None, "overall": None}
    facts = {
        "status": "complete",
        "competitors": [{"label": "rival.com", "social": 40}],
    }
    assert build_benchmark_report_data(scores=scores, benchmark_facts=facts) is None


def test_normalize_non_list_competitors_is_empty_not_raise() -> None:
    # A truthy non-list competitors value must degrade like a missing list — the docstring
    # promises direct callers are protected without an outer wrapper.
    for raw in ({"competitors": 1}, {"competitors": True}, {"competitors": "rival.com"}):
        facts = normalize_benchmark_facts(raw, provider="semrush", target_url="u", niche=None)
        assert facts["status"] == "empty"
        assert facts["competitors"] == []
