"""Apify social data backends (Phase 2).

Fetch public Instagram profiles (Instagram Scraper actor) and Facebook pages (Facebook
Pages Scraper actor) via Apify's synchronous run endpoint. Network-only; returns the raw
provider item or None so the collector degrades gracefully (like a missing PSI key). The
token is read from Settings and sent only as an ``Authorization: Bearer`` header — never in
the URL, where httpx's INFO request log (and any error text echoing the URL) would expose it.
Logged-out / public data only — no OAuth, no login to the target account.
"""

from __future__ import annotations

from typing import Any

import httpx
from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings
from apps.worker.stages.social.extractor import profile_link_from_handle

_IG_ACTOR = "apify~instagram-scraper"
_IG_ENDPOINT = f"https://api.apify.com/v2/acts/{_IG_ACTOR}/run-sync-get-dataset-items"
_IG_RESULTS_LIMIT = 12

_FB_ACTOR = "apify~facebook-pages-scraper"
_FB_ENDPOINT = f"https://api.apify.com/v2/acts/{_FB_ACTOR}/run-sync-get-dataset-items"

# The Pages actor returns page metadata only; this separate actor returns the page's posts
# (text, publish date, likes/reactions/comments) so FB gets cadence + engagement like IG.
_FB_POSTS_ACTOR = "apify~facebook-posts-scraper"
_FB_POSTS_ENDPOINT = f"https://api.apify.com/v2/acts/{_FB_POSTS_ACTOR}/run-sync-get-dataset-items"
_FB_POSTS_LIMIT = 12


def _run_actor_items(
    endpoint: str, body: dict[str, Any], settings: Settings
) -> list[dict[str, Any]] | None:
    token = settings.apify_api_token.get_secret_value() if settings.apify_api_token else ""
    if not token:
        return None
    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=settings.apify_timeout_seconds + 60,
        )
        if response.status_code >= 400:
            return None
        items = response.json()
    except SoftTimeLimitExceeded:
        # The worker is out of time: propagate so the task can mark the job failed honestly
        # instead of the hard limit killing it mid-pipeline (crawler/site_health convention).
        raise
    except Exception:
        return None
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else None


def _run_actor(endpoint: str, body: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    items = _run_actor_items(endpoint, body, settings)
    return items[0] if items else None


def _actor_url(handle: str, platform_base: str) -> str:
    """The actor's target URL for a handle: a URL-shaped handle (scheme'd, protocol-relative,
    or scheme-less — the one shared detector) passes through verbatim; a bare handle nests
    under the platform host. A startswith("http") check here once missed the scheme-less form
    and minted doubled-domain URLs like ``…facebook.com/www.facebook.com/acme/``."""
    link = profile_link_from_handle(handle)
    if link is not None:
        return link
    return f"{platform_base}/{handle.strip().lstrip('@').strip('/')}/"


def fetch_instagram_profile(handle: str, settings: Settings) -> dict[str, Any] | None:
    if not handle:
        return None
    url = _actor_url(handle, "https://www.instagram.com")
    body = {"directUrls": [url], "resultsType": "details", "resultsLimit": _IG_RESULTS_LIMIT}
    return _run_actor(_IG_ENDPOINT, body, settings)


def fetch_facebook_page(handle: str, settings: Settings) -> dict[str, Any] | None:
    if not handle:
        return None
    body = {
        "startUrls": [{"url": _actor_url(handle, "https://www.facebook.com")}],
        "resultsLimit": 1,
    }
    return _run_actor(_FB_ENDPOINT, body, settings)


def fetch_facebook_posts(handle: str, settings: Settings) -> list[dict[str, Any]] | None:
    if not handle:
        return None
    body = {
        "startUrls": [{"url": _actor_url(handle, "https://www.facebook.com")}],
        "resultsLimit": _FB_POSTS_LIMIT,
    }
    return _run_actor_items(_FB_POSTS_ENDPOINT, body, settings)
