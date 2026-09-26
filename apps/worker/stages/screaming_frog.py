from __future__ import annotations

import csv
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings
from apps.worker.stages.technical_crawl_common import (
    empty_summary,
    issues_from_summary,
    looks_like_post,
)

JsonDict = dict[str, Any]

SCREAMING_FROG_SOURCE = "screaming_frog_csv"

# Below this many seconds of remaining budget there is no point starting a crawl that
# would only time out; skip it so the site-health fallback can use the time instead.
_MIN_RUN_SECONDS = 30


def collect_screaming_frog_facts(
    url: str,
    audit_id: str,
    settings: Settings,
    *,
    deadline: float | None = None,
) -> JsonDict:
    started_at = _utc_now()
    if not settings.screaming_frog_enabled:
        return _skipped("disabled", started_at)

    binary = _resolve_binary(settings.screaming_frog_binary)
    if binary is None:
        return _failed("screaming_frog_binary_not_found", started_at)

    if not _is_http_url(url):
        return _failed("invalid_crawl_url", started_at)

    timeout_seconds = _effective_timeout_seconds(settings, deadline)
    if timeout_seconds < _MIN_RUN_SECONDS:
        return _skipped("insufficient_time_budget", started_at)

    output_dir = (settings.screaming_frog_output_dir / audit_id).resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failed(str(exc), started_at, output_dir=output_dir)
    command = [
        binary,
        "--crawl",
        url,
        "--headless",
        "--output-folder",
        str(output_dir),
        "--export-tabs",
        settings.screaming_frog_export_tabs,
        "--export-format",
        "csv",
        "--overwrite",
    ]

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _failed("screaming_frog_timeout", started_at, output_dir=output_dir)
    except OSError as exc:
        return _failed(str(exc), started_at, output_dir=output_dir)

    command_output = "\n".join(part for part in (completed.stderr, completed.stdout) if part)
    fatal_error = _fatal_error_from_output(command_output)
    try:
        parsed = parse_screaming_frog_exports(output_dir, site_url=url)
    except SoftTimeLimitExceeded:
        # Out of time: propagate so the task fails the job honestly instead of the hard limit
        # SIGKILLing the worker (crawler/site_health/provider convention).
        raise
    except Exception as exc:
        return _failed(_trim_error(str(exc)), started_at, output_dir=output_dir)
    parsed["started_at"] = started_at
    parsed["completed_at"] = _utc_now()
    parsed["output_dir"] = str(output_dir)
    parsed["command"] = _safe_command(command)
    parsed["exit_code"] = completed.returncode

    has_usable_exports = parsed["status"] == "complete" and bool(parsed.get("files"))
    if completed.returncode != 0 or (fatal_error and not has_usable_exports):
        parsed["status"] = "failed"
        parsed["error"] = fatal_error or _trim_error(command_output)
    elif fatal_error:
        parsed["warnings"] = [fatal_error]
    return parsed


def parse_screaming_frog_exports(export_dir: Path, site_url: str | None = None) -> JsonDict:
    csv_files = sorted(export_dir.rglob("*.csv"))
    if not csv_files:
        return {
            "status": "empty",
            "source": SCREAMING_FROG_SOURCE,
            "summary": {},
            "issues": [],
            "files": [],
        }

    pages: dict[str, JsonDict] = {}
    issue_examples: dict[str, list[str]] = defaultdict(list)
    images_missing_alt = 0
    files: list[JsonDict] = []

    for path in csv_files:
        rows = _read_csv(path)
        files.append({"path": str(path), "rows": len(rows)})
        file_key = _normalize_name(path.name)

        for row in rows:
            normalized = {_normalize_name(key): value for key, value in row.items()}
            address = _first_text(
                normalized,
                "address",
                "url",
                "source",
                "destination",
                "image_address",
            )
            if not address:
                continue

            if "image" in file_key or "missing_alt" in file_key:
                if _image_missing_alt(normalized, file_key):
                    images_missing_alt += 1
                    _append_example(issue_examples, "images_missing_alt", address)
                continue

            page = pages.setdefault(address, {"address": address})
            page.update(
                {key: value for key, value in normalized.items() if value not in {None, ""}}
            )

    summary = _summarize_pages(
        list(pages.values()),
        images_missing_alt,
        issue_examples,
        site_url=site_url,
    )
    issues = issues_from_summary(summary, issue_examples, source="screaming_frog")
    return {
        "status": "complete",
        "source": SCREAMING_FROG_SOURCE,
        "summary": summary,
        "issues": issues,
        "files": files,
    }


def _summarize_pages(
    pages: list[JsonDict],
    images_missing_alt: int,
    issue_examples: dict[str, list[str]],
    *,
    site_url: str | None = None,
) -> JsonDict:
    title_counter: Counter[str] = Counter()
    meta_counter: Counter[str] = Counter()
    summary = empty_summary()
    summary["urls_crawled"] = len(pages)
    summary["images_missing_alt"] = images_missing_alt
    discovered_internal = 0
    discovered_posts = 0

    for page in pages:
        address = str(page.get("address") or "")
        status_code = _int(_first_text(page, "status_code", "status"))
        is_html = _is_html_page(page)
        is_indexable_html_page = is_html and (status_code is None or 200 <= status_code < 300)
        if (
            is_html
            and _is_internal_url(address, site_url)
            and not (status_code is not None and 300 <= status_code < 400)
        ):
            # Website-scope counts, parity with the sweep's discovered_* fields: every internal
            # HTML URL the crawl saw feeds the report's "what your website consists of" panel.
            # 3xx rows are excluded — Screaming Frog lists a redirect source AND its target as
            # separate rows, and counting both would double-count every redirected page (the
            # exact inflation the sweep's final-URL counting avoids). Outbound/sitemap totals
            # aren't in the configured exports, so those rows stay absent and the panel simply
            # omits them for a Screaming Frog run.
            discovered_internal += 1
            if looks_like_post(address):
                discovered_posts += 1
        if status_code is not None:
            if 400 <= status_code <= 499:
                issue_key = (
                    "client_error_internal_urls"
                    if _is_internal_url(address, site_url)
                    else "client_error_external_urls"
                )
                summary[issue_key] += 1
                _append_example(issue_examples, issue_key, address)
            elif status_code >= 500:
                issue_key = (
                    "server_error_internal_urls"
                    if _is_internal_url(address, site_url)
                    else "server_error_external_urls"
                )
                summary[issue_key] += 1
                _append_example(issue_examples, issue_key, address)

        if not is_html:
            continue

        summary["html_urls_crawled"] += 1

        if not is_indexable_html_page:
            continue

        indexability = _first_text(page, "indexability")
        if indexability and indexability.lower() != "indexable":
            summary["non_indexable_internal_urls"] += 1
            _append_example(issue_examples, "non_indexable_internal_urls", address)

        title = _first_text(page, "title_1", "page_title_1", "title", "page_title")
        if title:
            title_counter[title.strip().lower()] += 1
        else:
            summary["missing_titles"] += 1
            _append_example(issue_examples, "missing_titles", address)

        meta = _first_text(
            page,
            "meta_description_1",
            "meta_description",
            "description_1",
            "description",
        )
        if meta:
            meta_counter[meta.strip().lower()] += 1
        else:
            summary["missing_meta_descriptions"] += 1
            _append_example(issue_examples, "missing_meta_descriptions", address)

        h1 = _first_text(page, "h1_1", "h1")
        if not h1:
            summary["missing_h1"] += 1
            _append_example(issue_examples, "missing_h1", address)

        canonical = _first_text(page, "canonical_link_element_1", "canonical")
        if not canonical:
            summary["missing_canonicals"] += 1
            _append_example(issue_examples, "missing_canonicals", address)

    duplicate_titles = {value for value, count in title_counter.items() if count > 1}
    duplicate_meta = {value for value, count in meta_counter.items() if count > 1}
    for page in pages:
        status_code = _int(_first_text(page, "status_code", "status"))
        if not _is_html_page(page) or (status_code is not None and not 200 <= status_code < 300):
            continue
        address = str(page.get("address") or "")
        title = _first_text(page, "title_1", "page_title_1", "title", "page_title")
        if title and title.strip().lower() in duplicate_titles:
            summary["duplicate_titles"] += 1
            _append_example(issue_examples, "duplicate_titles", address)

        meta = _first_text(
            page,
            "meta_description_1",
            "meta_description",
            "description_1",
            "description",
        )
        if meta and meta.strip().lower() in duplicate_meta:
            summary["duplicate_meta_descriptions"] += 1
            _append_example(issue_examples, "duplicate_meta_descriptions", address)

    summary["discovered_internal_urls"] = discovered_internal
    summary["discovered_blog_posts"] = discovered_posts

    return summary


def _read_csv(path: Path) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with path.open(newline="", encoding=encoding) as file:
                return list(csv.DictReader(file))
        except UnicodeDecodeError:
            continue
    return []


def _image_missing_alt(row: JsonDict, file_key: str) -> bool:
    if "missing_alt" in file_key:
        return True
    alt = _first_text(row, "alt_text", "alt", "alt_1", "image_alt_text")
    return not bool(alt)


def _first_text(row: JsonDict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        cleaned = " ".join(str(value).split())
        if cleaned:
            return cleaned
    return None


def _is_html_page(row: JsonDict) -> bool:
    content_type = _first_text(row, "content_type", "type")
    if not content_type:
        return True
    normalized = content_type.lower()
    return "html" in normalized


def _append_example(examples: dict[str, list[str]], key: str, address: str) -> None:
    if address and len(examples[key]) < 10 and address not in examples[key]:
        examples[key].append(address)


def _normalize_name(value: str | None) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "_" for char in str(value or ""))
    return "_".join(part for part in cleaned.split("_") if part)


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _effective_timeout_seconds(settings: Settings, deadline: float | None = None) -> int:
    """Clamp the subprocess timeout under the remaining audit time budget.

    The audit task must outlive the Screaming Frog subprocess so this stage can
    record a 'failed' payload and the rest of the audit can continue. Two minutes of
    the soft-limit budget are reserved for the remaining stages, and when a pipeline
    ``deadline`` is supplied the timeout is further bounded by the time actually left
    (a 20s slice is held back for the site-health fallback). The result may be small
    or negative; the caller skips the crawl when it drops below ``_MIN_RUN_SECONDS``.
    """
    configured = int(settings.screaming_frog_timeout_seconds)
    soft_limit = getattr(settings, "celery_task_soft_time_limit_seconds", None)
    cap = configured
    if isinstance(soft_limit, int | float):
        cap = min(cap, int(soft_limit) - 120)
    if deadline is not None:
        cap = min(cap, int(deadline - time.monotonic()) - 20)
    return cap


def _resolve_binary(configured: Path | None) -> str | None:
    if configured is None:
        return shutil.which("screamingfrogseospider")
    raw = str(configured)
    if "/" not in raw:
        return shutil.which(raw)
    return raw if configured.exists() else None


def _safe_command(command: list[str]) -> list[str]:
    return [part for part in command if part]


def _trim_error(value: str | None) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned[:500] or "Screaming Frog exited with an error."


def _fatal_error_from_output(value: str | None) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if "FATAL" not in cleaned and "SeoSpider failed to start" not in cleaned:
        return None
    if "Could not locate licence file" in cleaned:
        return (
            "Screaming Frog could not locate its licence file. Remove licensed-only "
            "CLI options or install a licence at ~/.ScreamingFrogSEOSpider/licence.txt."
        )
    if "Directory does not exist:" in cleaned:
        return "Screaming Frog output directory does not exist."
    return _trim_error(cleaned)


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@lru_cache(maxsize=8)
def _audited_site_host(site_url: str) -> str:
    """The audited site's comparison host, derived once per export instead of once per CSV row
    (the Internal:All export can carry thousands of rows, each calling _is_internal_url)."""
    return (urlparse(site_url).hostname or "").lower().removeprefix("www.")


def _is_internal_url(url: str, site_url: str | None) -> bool:
    if not site_url:
        return True
    site_host = _audited_site_host(site_url)
    url_host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return bool(site_host and (url_host == site_host or url_host.endswith(f".{site_host}")))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _skipped(reason: str, started_at: str) -> JsonDict:
    return {
        "status": "skipped",
        "source": "screaming_frog_cli",
        "reason": reason,
        "summary": {},
        "issues": [],
        "files": [],
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _failed(reason: str, started_at: str, output_dir: Path | None = None) -> JsonDict:
    payload = {
        "status": "failed",
        "source": "screaming_frog_cli",
        "error": reason,
        "summary": {},
        "issues": [],
        "files": [],
        "started_at": started_at,
        "completed_at": _utc_now(),
    }
    if output_dir is not None:
        payload["output_dir"] = str(output_dir)
    return payload
