"""YouTube Analytics (connected/owner) normalizer + graceful fetch (SAE-15, Wave 3).

Covers the pure normalization of reports.query payloads. The live fetch path (OAuth token ->
network) is exercised only against a real connected channel and is not unit-tested here.
"""

import json
from pathlib import Path

from apps.shared.config import Settings
from apps.worker.stages.social.youtube_analytics_provider import (
    fetch_channel_analytics,
    normalize_youtube_analytics,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_normalizer_flattens_reports() -> None:
    raw = json.loads((FIXTURES / "youtube_analytics_reports.json").read_text())
    facts = normalize_youtube_analytics(raw)
    assert facts["views"] == 15234
    assert facts["estimated_minutes_watched"] == 48210
    assert facts["avg_view_duration_seconds"] == 190
    assert facts["avg_view_percentage"] == 42.7
    assert facts["subscribers_gained"] == 320
    assert facts["subscribers_lost"] == 45
    assert facts["traffic_sources"][0] == {"source": "YT_SEARCH", "views": 8200}
    assert facts["demographics"][0] == {
        "age_group": "age25-34",
        "gender": "male",
        "viewer_pct": 31.4,
    }


def test_normalizer_is_none_safe_on_empty_reports() -> None:
    facts = normalize_youtube_analytics({})
    assert facts["views"] is None
    assert facts["traffic_sources"] == []
    assert facts["demographics"] == []


def test_fetch_returns_none_without_token() -> None:
    assert (
        fetch_channel_analytics(
            "", start_date="2026-01-01", end_date="2026-03-31", settings=Settings()
        )
        is None
    )


def test_oauth_scopes_gate_youtube_analytics() -> None:
    from apps.worker.stages.google_search_console import (
        GSC_SCOPES,
        YOUTUBE_ANALYTICS_SCOPES,
        oauth_scopes,
    )

    # Default: connected-mode off -> Search Console scopes only (existing behaviour unchanged).
    # _env_file=None isolates from the local .env (which may set the toggle for live testing).
    assert oauth_scopes(Settings(_env_file=None)) == GSC_SCOPES
    # Enabled -> the YouTube Analytics scopes are appended to the same consent.
    enabled = oauth_scopes(Settings(_env_file=None, youtube_analytics_connect_enabled=True))
    assert enabled == GSC_SCOPES + YOUTUBE_ANALYTICS_SCOPES
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in enabled


def test_connected_youtube_injector_gates_then_injects(monkeypatch) -> None:
    # SMWA-140 pipeline wiring: every gate must hold — flag, a YouTube handle on the audit,
    # a Google connection whose GRANT includes the YT scopes, AND the connected channel being
    # the audited handle's channel — then the normalized facts land under
    # social_facts["youtube_analytics"]. Any miss leaves the facts untouched.
    from types import SimpleNamespace

    from apps.worker import tasks
    from apps.worker.stages.google_search_console import YOUTUBE_ANALYTICS_SCOPES

    raw = json.loads((FIXTURES / "youtube_analytics_reports.json").read_text())
    handles = {"youtube": "@acmestudio"}

    # Gate 1: flag off (the default) -> untouched.
    facts: dict = {"status": "complete"}
    tasks._augment_with_connected_youtube(
        facts, SimpleNamespace(youtube_analytics_connect_enabled=False), db=None, handles=handles
    )
    assert "youtube_analytics" not in facts

    # Gate 2: flag on but no YouTube handle on the audit -> untouched.
    flag_on = SimpleNamespace(youtube_analytics_connect_enabled=True)
    tasks._augment_with_connected_youtube(facts, flag_on, db=None, handles={"instagram": "acme"})
    assert "youtube_analytics" not in facts

    # Gate 3: connection granted only the GSC scopes (pre-flag consent) -> untouched.
    gsc_only = SimpleNamespace(
        scopes={"values": ["https://www.googleapis.com/auth/webmasters.readonly"]}
    )
    monkeypatch.setattr(tasks, "latest_google_connection", lambda db: gsc_only)
    tasks._augment_with_connected_youtube(facts, flag_on, db=None, handles=handles)
    assert "youtube_analytics" not in facts

    granted = SimpleNamespace(scopes={"values": list(YOUTUBE_ANALYTICS_SCOPES)})
    monkeypatch.setattr(tasks, "latest_google_connection", lambda db: granted)
    monkeypatch.setattr(tasks, "ensure_google_access_token", lambda connection, settings, db: "tok")
    monkeypatch.setattr(
        tasks,
        "fetch_channel_analytics",
        lambda token, *, start_date, end_date, settings: raw,
    )

    # Gate 4 (identity): the Analytics API only reports on the CONNECTED account's channel
    # (ids=channel==MINE) — a connected channel that is NOT the audited handle's (e.g. the
    # operator's own account, connected once for GSC) must never inject its private metrics
    # into the client's audit.
    stranger = {"id": "UCstranger000000000000000", "custom_url": "@operatorchannel", "title": "Op"}
    monkeypatch.setattr(tasks, "fetch_own_channel", lambda token, settings: stranger)
    tasks._augment_with_connected_youtube(facts, flag_on, db=None, handles=handles)
    assert "youtube_analytics" not in facts

    # Unverifiable identity (channels.list failed) -> conservative: untouched.
    monkeypatch.setattr(tasks, "fetch_own_channel", lambda token, settings: None)
    tasks._augment_with_connected_youtube(facts, flag_on, db=None, handles=handles)
    assert "youtube_analytics" not in facts

    # Happy path: all gates pass (connected channel IS the audited handle's) -> normalized
    # facts injected with a window + the verified channel identity for the report meta.
    own = {"id": "UCacme0000000000000000000", "custom_url": "@acmestudio", "title": "Acme Studio"}
    monkeypatch.setattr(tasks, "fetch_own_channel", lambda token, settings: own)
    tasks._augment_with_connected_youtube(facts, flag_on, db=None, handles=handles)
    injected = facts["youtube_analytics"]
    assert injected["status"] == "complete"
    assert injected["views"] == 15234
    assert injected["window"]["start_date"] < injected["window"]["end_date"]
    assert injected["channel"] == {
        "id": "UCacme0000000000000000000",
        "handle": "@acmestudio",
        "title": "Acme Studio",
    }


def test_report_builder_precomposes_connected_youtube_lines() -> None:
    # SMWA-142 (minimal): the shared builder emits ONE precomposed block (meta + lines) that
    # the PDF, DOCX, and web UI all render verbatim — and None when nothing was collected,
    # so no surface shows a dangling heading.
    from apps.worker.stages.social.report import build_social_report_data

    raw = json.loads((FIXTURES / "youtube_analytics_reports.json").read_text())
    from apps.worker.stages.social.youtube_analytics_provider import normalize_youtube_analytics

    facts = {
        "status": "complete",
        "platforms": [],
        "summary": {},
        "youtube_analytics": {
            "status": "complete",
            "window": {"start_date": "2026-03-01", "end_date": "2026-05-29"},
            **normalize_youtube_analytics(raw),
        },
    }
    report = build_social_report_data(
        social_facts=facts, social_breakdown={}, social_score=None, handles={}
    )
    block = report["connected_youtube"]
    assert block["meta"].startswith("Owner-consent YouTube Analytics")
    assert "2026-03-01 to 2026-05-29" in block["meta"]
    assert any(line.startswith("Views: 15234") for line in block["lines"])
    assert any("Subscribers: +320 / -45" in line for line in block["lines"])
    assert any("YT_SEARCH" in line for line in block["lines"])

    bare = build_social_report_data(
        social_facts={"status": "complete", "platforms": [], "summary": {}},
        social_breakdown={},
        social_score=None,
        handles={},
    )
    assert bare["connected_youtube"] is None
