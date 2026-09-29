"""SEARCH_CONSOLE_ENABLED=false (the Rick edition) cuts Google Search Console out entirely: no
Google calls, no OAuth routes, and no Search Console block on the PDF, DOCX or UI payload, even
for a stored result that carries Search Console data. Scores are untouched."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from fastapi.testclient import TestClient
from pypdf import PdfReader

from apps.api.main import create_app
from apps.shared.config import Settings
from apps.worker.stages import external_seo
from apps.worker.stages.docx_renderer import render_report_docx
from apps.worker.stages.pdf_renderer import render_report_pdf
from apps.worker.stages.report_payload import SEARCH_CONSOLE_RULE_PREFIX, compose_report_payload
from tests.unit.test_report_payload import _job, _result, _rule, _score_breakdown


def _settings(tmp_path: Path | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None}
    if tmp_path is not None:
        values["local_report_storage_dir"] = tmp_path
    values.update(overrides)
    return Settings(**values)


def _result_with_search_console() -> Any:
    # _result()'s external facts carry COMPLETE Search Console data; add a Search Console rule.
    breakdown = _score_breakdown()
    breakdown["categories"]["seo"]["rules"].append(
        _rule("seo.gsc.low_ctr_pages", "Search Console pages earn clicks.", "skipped", 0, 0)
    )
    return _result(score_breakdown=breakdown)


def _is_search_console_rule(rule: Any) -> bool:
    return rule.rule_id.startswith(SEARCH_CONSOLE_RULE_PREFIX)


def test_disabled_search_console_hides_it_from_the_payload_but_keeps_scores() -> None:
    result = _result_with_search_console()
    on = compose_report_payload(_job(), result, settings=_settings())
    off = compose_report_payload(_job(), result, settings=_settings(search_console_enabled=False))

    assert on.show_search_console and not off.show_search_console
    assert [card.score for card in off.scores] == [card.score for card in on.scores]
    assert any(_is_search_console_rule(rule) for rule in on.appendix.seo_rules)
    assert not any(_is_search_console_rule(rule) for rule in off.appendix.seo_rules)
    assert not any(
        _is_search_console_rule(rule) for section in off.sections for rule in section.opportunities
    )
    assert all("Search Console" not in card.description for card in off.scores)


def test_disabled_search_console_leaves_no_trace_in_the_pdf_or_docx(tmp_path) -> None:
    result = _result_with_search_console()
    texts: dict[bool, str] = {}
    for enabled in (True, False):
        settings = _settings(tmp_path, search_console_enabled=enabled)
        payload = compose_report_payload(_job(), result, settings=settings)
        pdf, docx = tmp_path / f"{enabled}.pdf", tmp_path / f"{enabled}.docx"
        render_report_pdf(payload, settings=settings, output_path=pdf)
        render_report_docx(payload, output_path=docx)
        with ZipFile(docx) as archive:
            document = re.sub(r"<[^>]+>", " ", archive.read("word/document.xml").decode())
        pdf_text = " ".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)
        texts[enabled] = f"{pdf_text}\n{document}".lower()

    assert "search console" in texts[True]
    assert "search console" not in texts[False]


def test_disabled_search_console_never_calls_google(monkeypatch) -> None:
    def must_not_run(**_kwargs: Any) -> None:
        raise AssertionError("Search Console must not be called when it is disabled")

    monkeypatch.setattr(external_seo, "collect_google_search_console_facts", must_not_run)
    monkeypatch.setattr(
        external_seo, "_collect_technical_crawl", lambda **_kwargs: {"status": "skipped"}
    )
    facts = external_seo.collect_external_seo_facts(
        url="https://example.com/",
        audit_id="audit",
        page_urls=[],
        settings=_settings(search_console_enabled=False),
        db=None,
    )
    assert facts["gsc"]["status"] == "skipped" and facts["gsc"]["reason"] == "disabled"
    assert facts["url_inspection"]["reason"] == "disabled"


def test_disabled_search_console_mounts_no_oauth_routes() -> None:
    off = TestClient(create_app(_settings(search_console_enabled=False)))
    assert off.get("/google/search-console/connect-url").status_code == 404
    assert off.get("/google/search-console/callback").status_code == 404
    on = TestClient(create_app(_settings()))
    assert on.get("/google/search-console/connect-url").status_code != 404
