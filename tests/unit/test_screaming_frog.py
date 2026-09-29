from types import SimpleNamespace

from apps.worker.stages.screaming_frog import (
    collect_screaming_frog_facts,
    parse_screaming_frog_exports,
)


def test_parse_screaming_frog_exports_counts_technical_issues(tmp_path) -> None:
    (tmp_path / "internal_all.csv").write_text(
        "\n".join(
            [
                "Address,Status Code,Content Type,Indexability,Title 1,Meta Description 1,"
                "H1-1,Canonical Link Element 1",
                "https://example.com/,200,text/html,Indexable,Home,Welcome,Home,"
                "https://example.com/",
                "https://example.com/broken,404,text/html,Non-Indexable,,,,",
                "https://example.com/noindex,200,text/html,Non-Indexable,Noindex,Noindex,Noindex,",
                "https://example.com/missing,200,text/html,Indexable,,,Missing heading,",
                "https://example.com/dup-a,200,text/html,Indexable,Same,Meta,A,",
                "https://example.com/dup-b,200,text/html,Indexable,Same,Meta,B,",
                "https://example.com/photo.jpg,200,image/jpeg,Indexable,,,,",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "images_missing_alt.csv").write_text(
        "Image Address,Alt Text\nhttps://example.com/photo.jpg,\n",
        encoding="utf-8",
    )

    parsed = parse_screaming_frog_exports(tmp_path)

    summary = parsed["summary"]
    assert parsed["status"] == "complete"
    assert summary["urls_crawled"] == 7
    assert summary["html_urls_crawled"] == 6
    assert summary["client_error_internal_urls"] == 1
    assert summary["non_indexable_internal_urls"] == 1
    # Website-scope parity with the sweep: a Screaming Frog run must still feed the
    # "what your website consists of" panel (internal HTML URLs + the blog-post heuristic).
    assert summary["discovered_internal_urls"] == 6
    assert summary["discovered_blog_posts"] == 0
    assert summary["missing_titles"] == 1
    assert summary["duplicate_titles"] == 2
    assert summary["missing_meta_descriptions"] == 1
    assert summary["duplicate_meta_descriptions"] == 2
    assert summary["images_missing_alt"] == 1
    issue_ids = {issue["id"] for issue in parsed["issues"]}
    assert "client_error_internal_urls" in issue_ids
    assert "images_missing_alt" in issue_ids
    assert "duplicate_meta_descriptions" in issue_ids
    missing_title_issue = next(
        issue for issue in parsed["issues"] if issue["id"] == "missing_titles"
    )
    assert "https://example.com/photo.jpg" not in missing_title_issue["examples"]


def test_scope_count_excludes_redirect_rows(tmp_path) -> None:
    # Screaming Frog lists a redirect source AND its target as separate rows; counting both
    # would double-count every redirected page in the client-facing "Pages discovered".
    (tmp_path / "internal_all.csv").write_text(
        "\n".join(
            [
                "Address,Status Code,Content Type,Indexability,Title 1,Meta Description 1,"
                "H1-1,Canonical Link Element 1",
                "http://example.com/,301,,Non-Indexable,,,,",
                "https://www.example.com/,200,text/html,Indexable,Home,Welcome,Home,"
                "https://www.example.com/",
            ]
        ),
        encoding="utf-8",
    )
    parsed = parse_screaming_frog_exports(tmp_path, site_url="https://www.example.com/")
    assert parsed["summary"]["discovered_internal_urls"] == 1


def test_parse_screaming_frog_exports_ignores_redirect_metadata_and_splits_external_errors(
    tmp_path,
) -> None:
    (tmp_path / "internal_all.csv").write_text(
        "\n".join(
            [
                "Address,Status Code,Status,Content Type,Indexability,Indexability Status,"
                "Title 1,Meta Description 1,H1-1,Canonical Link Element 1,Redirect URL",
                "https://example.com/,200,OK,text/html,Indexable,,Home,Welcome,Home,"
                "https://example.com/,",
                "https://example.com/old,301,Moved Permanently,text/html,Non-Indexable,"
                "Redirected,,,,,https://example.com/new",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "response_codes_client_error_(4xx).csv").write_text(
        "Address,Status Code,Status,Content Type\n"
        "https://open.spotify.com/show/abc,403,Forbidden,text/html\n",
        encoding="utf-8",
    )

    parsed = parse_screaming_frog_exports(tmp_path, site_url="https://example.com/")

    summary = parsed["summary"]
    assert summary["missing_titles"] == 0
    assert summary["missing_meta_descriptions"] == 0
    assert summary["missing_h1"] == 0
    assert summary["missing_canonicals"] == 0
    assert summary["non_indexable_internal_urls"] == 0
    assert summary["client_error_internal_urls"] == 0
    assert summary["client_error_external_urls"] == 1
    issue_ids = {issue["id"] for issue in parsed["issues"]}
    assert "client_error_external_urls" in issue_ids
    assert "client_error_internal_urls" not in issue_ids


def test_collect_screaming_frog_facts_uses_fake_cli_exports(tmp_path) -> None:
    fake_cli = tmp_path / "fake-screaming-frog"
    fake_cli.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

output_dir = Path(sys.argv[sys.argv.index("--output-folder") + 1])
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / "internal_all.csv").write_text(
    "Address,Status Code,Indexability,Title 1,Meta Description 1,H1-1,Canonical Link Element 1\\n"
    "https://example.com/,200,Indexable,Home,Welcome,Home,https://example.com/\\n",
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    settings = SimpleNamespace(
        screaming_frog_enabled=True,
        screaming_frog_binary=fake_cli,
        screaming_frog_output_dir=tmp_path / "exports",
        screaming_frog_timeout_seconds=30,
        screaming_frog_export_tabs="Internal:All",
    )

    facts = collect_screaming_frog_facts(
        "https://example.com/",
        "audit-123",
        settings,
    )

    assert facts["status"] == "complete"
    assert facts["summary"]["urls_crawled"] == 1
    assert facts["exit_code"] == 0
    assert "--save-crawl" not in facts["command"]
    assert "--overwrite" in facts["command"]


def test_collect_screaming_frog_facts_fails_on_fatal_output_with_zero_exit(
    tmp_path,
) -> None:
    fake_cli = tmp_path / "fake-screaming-frog"
    fake_cli.write_text(
        """#!/usr/bin/env python3
print("FATAL - SeoSpider failed to start")
print("Could not locate licence file, please check file exists.")
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    settings = SimpleNamespace(
        screaming_frog_enabled=True,
        screaming_frog_binary=fake_cli,
        screaming_frog_output_dir=tmp_path / "exports",
        screaming_frog_timeout_seconds=30,
        screaming_frog_export_tabs="Internal:All",
    )

    facts = collect_screaming_frog_facts(
        "https://example.com/",
        "audit-123",
        settings,
    )

    assert facts["status"] == "failed"
    assert facts["exit_code"] == 0
    assert "licence file" in facts["error"]
    assert facts["summary"] == {}


def test_collect_screaming_frog_facts_keeps_exports_when_log_contains_fatal(
    tmp_path,
) -> None:
    fake_cli = tmp_path / "fake-screaming-frog"
    fake_cli.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

output_dir = Path(sys.argv[sys.argv.index("--output-folder") + 1])
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / "internal_all.csv").write_text(
    "Address,Status Code,Indexability,Title 1,Meta Description 1,H1-1,Canonical Link Element 1\\n"
    "https://example.com/,200,Indexable,Home,Welcome,Home,https://example.com/\\n",
    encoding="utf-8",
)
print("FATAL - late non-blocking CLI log after export")
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    settings = SimpleNamespace(
        screaming_frog_enabled=True,
        screaming_frog_binary=fake_cli,
        screaming_frog_output_dir=tmp_path / "exports",
        screaming_frog_timeout_seconds=30,
        screaming_frog_export_tabs="Internal:All",
    )

    facts = collect_screaming_frog_facts(
        "https://example.com/",
        "audit-123",
        settings,
    )

    assert facts["status"] == "complete"
    assert facts["summary"]["urls_crawled"] == 1
    assert facts["warnings"]


def test_collect_screaming_frog_facts_skips_when_disabled(tmp_path) -> None:
    settings = SimpleNamespace(
        screaming_frog_enabled=False,
        screaming_frog_binary=None,
        screaming_frog_output_dir=tmp_path,
        screaming_frog_timeout_seconds=30,
        screaming_frog_export_tabs="Internal:All",
    )

    facts = collect_screaming_frog_facts("https://example.com/", "audit-123", settings)

    assert facts["status"] == "skipped"
    assert facts["reason"] == "disabled"
    assert facts["summary"] == {}


def test_empty_screaming_frog_exports_do_not_emit_clean_zero_summary(tmp_path) -> None:
    parsed = parse_screaming_frog_exports(tmp_path, site_url="https://example.com/")

    assert parsed["status"] == "empty"
    assert parsed["summary"] == {}
