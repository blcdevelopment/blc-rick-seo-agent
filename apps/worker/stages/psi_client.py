from __future__ import annotations

import copy
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from apps.shared.config import Settings

JsonDict = dict[str, Any]
MappingLike = dict[str, Any]
PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_STRATEGIES = ("mobile", "desktop")
PSI_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
_CACHE_MAX_ENTRIES = 512
_CACHE: dict[tuple[str, str], tuple[float, JsonDict]] = {}


class PageSpeedError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _score_to_percent(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # Half-up rounding to match the scoring engine and averages (scoring._round_score,
    # _average) so the codebase uses one rounding convention.
    return int(value * 100 + 0.5)


def _audit_numeric(audits: MappingLike, audit_id: str) -> float | None:
    value = audits.get(audit_id, {}).get("numericValue")
    return float(value) if isinstance(value, int | float) else None


def _audit_score(audits: MappingLike, audit_id: str) -> int | None:
    return _score_to_percent(audits.get(audit_id, {}).get("score"))


# CrUX field metrics carried inside the PSI response (loadingExperience /
# originLoadingExperience). Maps our snake_case key -> the PSI metric id.
_CRUX_FIELD_METRICS = {
    "largest_contentful_paint_ms": "LARGEST_CONTENTFUL_PAINT_MS",
    "interaction_to_next_paint_ms": "INTERACTION_TO_NEXT_PAINT",
    "cumulative_layout_shift": "CUMULATIVE_LAYOUT_SHIFT_SCORE",
    "first_contentful_paint_ms": "FIRST_CONTENTFUL_PAINT_MS",
    "time_to_first_byte_ms": "EXPERIMENTAL_TIME_TO_FIRST_BYTE",
}


def _crux_metric(metrics: MappingLike, our_key: str, api_key: str) -> JsonDict | None:
    metric = metrics.get(api_key)
    if not isinstance(metric, dict):
        return None
    percentile = metric.get("percentile")
    category = metric.get("category")
    if not isinstance(percentile, int | float):
        return {"p75": None, "category": category} if category else None
    # PSI reports the CLS field percentile as an integer scaled by 100 (e.g. 3 -> 0.03),
    # unlike the lab CLS which is already a unitless fraction. Normalize to real units here.
    p75 = round(percentile / 100, 3) if our_key == "cumulative_layout_shift" else percentile
    return {"p75": p75, "category": category}


def _crux_experience(experience: Any) -> JsonDict | None:
    """Normalize a PSI loadingExperience / originLoadingExperience block (real-user
    CrUX field data). Returns None when no usable field data is present."""
    if not isinstance(experience, dict):
        return None
    metrics = experience.get("metrics") or {}
    overall = experience.get("overall_category")
    result: JsonDict = {"overall_category": overall}
    has_metric = False
    for our_key, api_key in _CRUX_FIELD_METRICS.items():
        metric = _crux_metric(metrics, our_key, api_key)
        result[our_key] = metric
        has_metric = has_metric or metric is not None
    if not has_metric and not overall:
        return None
    return result


def normalize_pagespeed_response(payload: JsonDict, strategy: str) -> JsonDict:
    lighthouse = payload.get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}

    return {
        "status": "complete",
        "strategy": strategy,
        "analysis_url": payload.get("id"),
        "final_url": lighthouse.get("finalDisplayedUrl") or lighthouse.get("finalUrl"),
        "fetch_time": lighthouse.get("fetchTime"),
        "scores": {
            "performance": _score_to_percent(categories.get("performance", {}).get("score")),
            "accessibility": _score_to_percent(categories.get("accessibility", {}).get("score")),
            "best_practices": _score_to_percent(categories.get("best-practices", {}).get("score")),
            "seo": _score_to_percent(categories.get("seo", {}).get("score")),
        },
        "lab_metrics": {
            "first_contentful_paint_ms": _audit_numeric(audits, "first-contentful-paint"),
            "largest_contentful_paint_ms": _audit_numeric(
                audits,
                "largest-contentful-paint",
            ),
            "speed_index_ms": _audit_numeric(audits, "speed-index"),
            "total_blocking_time_ms": _audit_numeric(audits, "total-blocking-time"),
            "cumulative_layout_shift": _audit_numeric(audits, "cumulative-layout-shift"),
        },
        "audit_scores": {
            "uses_responsive_images": _audit_score(audits, "uses-responsive-images"),
            "modern_image_formats": _audit_score(audits, "modern-image-formats"),
            "offscreen_images": _audit_score(audits, "offscreen-images"),
            "render_blocking_resources": _audit_score(audits, "render-blocking-resources"),
            "meta_description": _audit_score(audits, "meta-description"),
            "document_title": _audit_score(audits, "document-title"),
        },
        # Real-user (CrUX) field data carried in the same PSI response. ``page`` is this
        # exact URL's field data (often absent for low-traffic pages); ``origin`` is the
        # whole-site field data Google shows as "this origin".
        "field_data": {
            "page": _crux_experience(payload.get("loadingExperience")),
            "origin": _crux_experience(payload.get("originLoadingExperience")),
        },
    }


def _cache_get(url: str, strategy: str, ttl_seconds: int) -> JsonDict | None:
    if ttl_seconds <= 0:
        return None
    cached = _CACHE.get((url, strategy))
    if cached is None:
        return None
    cached_at, value = cached
    if time.time() - cached_at > ttl_seconds:
        _CACHE.pop((url, strategy), None)
        return None
    result = copy.deepcopy(value)
    result["cache_hit"] = True
    return result


def _cache_set(url: str, strategy: str, value: JsonDict, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    if len(_CACHE) >= _CACHE_MAX_ENTRIES and (url, strategy) not in _CACHE:
        oldest_key = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest_key, None)
    _CACHE[(url, strategy)] = (time.time(), copy.deepcopy(value))


def _build_params(url: str, strategy: str) -> list[tuple[str, str]]:
    params = [("url", url), ("strategy", strategy), ("utm_source", "blc-website-audit")]
    params.extend(("category", category) for category in PSI_CATEGORIES)
    return params


def _coerce_urls(urls: str | Sequence[str]) -> list[str]:
    if isinstance(urls, str):
        return [urls]

    seen: set[str] = set()
    page_urls: list[str] = []
    for url in urls:
        normalized_url = str(url).strip()
        if normalized_url and normalized_url not in seen:
            seen.add(normalized_url)
            page_urls.append(normalized_url)
    return page_urls


def _selected_urls(urls: list[str], settings: Settings) -> list[str]:
    if settings.psi_scope == "homepage":
        return urls[:1]

    return urls[: _max_pages(settings)]


def _max_pages(settings: Settings) -> int:
    if settings.psi_scope == "homepage":
        return 1
    return min(settings.psi_max_pages, settings.crawler_max_pages)


def _performance_score(page: JsonDict, strategy: str) -> int | None:
    value = page.get(strategy, {}).get("scores", {}).get("performance")
    return value if isinstance(value, int) else None


def _average(values: list[int]) -> int | None:
    if not values:
        return None
    return int((sum(values) / len(values)) + 0.5)


def _page_average(page: JsonDict) -> int | None:
    scores = [
        score
        for score in (
            _performance_score(page, "mobile"),
            _performance_score(page, "desktop"),
        )
        if score is not None
    ]
    return _average(scores)


def _origin_field_data(pages: list[JsonDict]) -> JsonDict | None:
    """The whole-site CrUX field data ("this origin") — identical across pages/strategies and
    more reliably present than per-URL field data, so it is the basis for the Core Web Vitals
    rules. Returns the first one found, or None when no page carries origin field data."""
    for page in pages:
        for strategy in PSI_STRATEGIES:
            field = page.get(strategy, {}).get("field_data", {})
            origin = field.get("origin") if isinstance(field, dict) else None
            if isinstance(origin, dict):
                return origin
    return None


def _field_p75(field: JsonDict | None, key: str) -> float | None:
    metric = field.get(key) if isinstance(field, dict) else None
    return metric.get("p75") if isinstance(metric, dict) else None


def _summarize_pages(pages: list[JsonDict]) -> JsonDict:
    mobile_scores = [
        score for page in pages if (score := _performance_score(page, "mobile")) is not None
    ]
    desktop_scores = [
        score for page in pages if (score := _performance_score(page, "desktop")) is not None
    ]
    slowest_pages = sorted(
        (
            {
                "url": page["url"],
                "mobile_performance": _performance_score(page, "mobile"),
                "desktop_performance": _performance_score(page, "desktop"),
                "average_performance": page_average,
            }
            for page in pages
            if (page_average := _page_average(page)) is not None
        ),
        key=lambda value: value["average_performance"],
    )

    # Real-user Core Web Vitals (75th-percentile) from the whole-site CrUX origin record,
    # in real units (LCP/INP in ms, CLS unitless). None when the site has no field data —
    # the CWV rules carry skip_if_missing so that never penalizes the score.
    origin = _origin_field_data(pages)
    crux = {
        "has_field_data": origin is not None,
        "overall_category": origin.get("overall_category") if origin else None,
        "lcp_p75_ms": _field_p75(origin, "largest_contentful_paint_ms"),
        "inp_p75_ms": _field_p75(origin, "interaction_to_next_paint_ms"),
        "cls_p75": _field_p75(origin, "cumulative_layout_shift"),
    }

    return {
        "avg_mobile_performance": _average(mobile_scores),
        "avg_desktop_performance": _average(desktop_scores),
        "complete_mobile_pages": len(mobile_scores),
        "complete_desktop_pages": len(desktop_scores),
        "slowest_pages": slowest_pages[:3],
        "crux": crux,
    }


def _fetch_strategy(url: str, strategy: str, settings: Settings) -> JsonDict:
    cached = _cache_get(url, strategy, settings.psi_cache_ttl_seconds)
    if cached is not None:
        return cached

    api_key = settings.google_psi_api_key.get_secret_value() if settings.google_psi_api_key else ""
    timeout = httpx.Timeout(settings.psi_timeout_seconds)
    last_error: str | None = None

    with httpx.Client(timeout=timeout) as client:
        for attempt in range(1, settings.psi_max_retries + 1):
            try:
                response = client.get(
                    PAGESPEED_ENDPOINT,
                    params=_build_params(url, strategy),
                    headers={
                        "User-Agent": settings.crawler_user_agent,
                        "x-goog-api-key": api_key,
                    },
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise PageSpeedError(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    return {
                        "status": "failed",
                        "strategy": strategy,
                        "error": f"PageSpeed returned HTTP {response.status_code}",
                    }

                normalized = normalize_pagespeed_response(response.json(), strategy)
                normalized["cache_hit"] = False
                _cache_set(url, strategy, normalized, settings.psi_cache_ttl_seconds)
                return normalized
            except (httpx.HTTPError, ValueError, PageSpeedError) as exc:
                last_error = str(exc)
                if attempt < settings.psi_max_retries:
                    time.sleep(min(2 ** (attempt - 1), 8))

    return {
        "status": "failed",
        "strategy": strategy,
        "error": last_error or "PageSpeed request failed",
    }


def collect_pagespeed_facts(urls: str | Sequence[str], settings: Settings) -> JsonDict:
    started_at = _utc_now()
    page_urls = _coerce_urls(urls)
    selected_urls = _selected_urls(page_urls, settings)

    if not selected_urls:
        return {
            "status": "skipped",
            "reason": "no_pages_to_analyze",
            "scope": settings.psi_scope,
            "homepage_url": None,
            "max_pages": _max_pages(settings),
            "pages_requested": len(page_urls),
            "pages_analyzed": 0,
            "pages": [],
            "strategies": {},
            "summary": _summarize_pages([]),
            "started_at": started_at,
            "completed_at": _utc_now(),
        }

    if not settings.google_psi_api_key or not settings.google_psi_api_key.get_secret_value():
        return {
            "status": "skipped",
            "reason": "missing_google_psi_api_key",
            "scope": settings.psi_scope,
            "homepage_url": selected_urls[0],
            "max_pages": _max_pages(settings),
            "pages_requested": len(page_urls),
            "pages_analyzed": 0,
            "pages": [],
            "strategies": {},
            "summary": _summarize_pages([]),
            "started_at": started_at,
            "completed_at": _utc_now(),
        }

    pages = []
    completed = 0
    budget = settings.psi_total_budget_seconds
    deadline = time.monotonic() + budget if budget and budget > 0 else None
    truncated = False
    for url in selected_urls:
        if deadline is not None and time.monotonic() >= deadline:
            # Out of time budget: stop issuing new PageSpeed requests and report on
            # the pages collected so far rather than letting the audit overrun the
            # Celery soft time limit.
            truncated = True
            break
        strategies = {
            strategy: _fetch_strategy(url, strategy, settings) for strategy in PSI_STRATEGIES
        }
        completed += sum(1 for value in strategies.values() if value.get("status") == "complete")
        pages.append(
            {
                "url": url,
                "mobile": strategies["mobile"],
                "desktop": strategies["desktop"],
            }
        )

    expected = len(pages) * len(PSI_STRATEGIES)
    if completed == expected and not truncated:
        status = "complete"
    elif completed:
        status = "partial"
    else:
        status = "failed"

    return {
        "status": status,
        "scope": settings.psi_scope,
        "homepage_url": selected_urls[0],
        "max_pages": _max_pages(settings),
        "pages_requested": len(page_urls),
        "pages_analyzed": len(pages),
        "pages": pages,
        "strategies": {
            strategy: pages[0][strategy]
            for strategy in PSI_STRATEGIES
            if pages and strategy in pages[0]
        },
        "summary": _summarize_pages(pages),
        "started_at": started_at,
        "completed_at": _utc_now(),
    }
