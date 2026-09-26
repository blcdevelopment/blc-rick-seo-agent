"""Live Apify probe for the Phase-2 social audit.

Runs the Instagram Scraper actor on one public profile to (a) confirm APIFY_API_TOKEN
works and (b) reveal the data shape that ``social/extractor.py`` must normalize into the
``social.*`` facts that ``rubrics/social.yaml`` scores. Free-tier friendly. The token is
read from .env via Settings and is NEVER printed.

Run from the repo root (token in ``.env``):

    python scripts/check_apify_social.py [instagram_handle_or_url]
    (defaults to the public @instagram account)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.shared.config import get_settings  # noqa: E402
from apps.worker.stages.social.apify_provider import _actor_url  # noqa: E402

ACTOR = "apify~instagram-scraper"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"

PROFILE_FIELDS = (
    "username",
    "fullName",
    "biography",
    "followersCount",
    "followsCount",
    "postsCount",
    "verified",
    "private",
    "businessCategoryName",
    "externalUrl",
)
POST_ENGAGEMENT_FIELDS = ("type", "likesCount", "commentsCount", "videoViewCount", "timestamp")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    raw = (args[0] if args else "instagram").strip()
    # The one shared URL-shaped-handle detector (a startswith("http") check here missed the
    # scheme-less form and probed a doubled-domain URL).
    url = _actor_url(raw, "https://www.instagram.com")

    settings = get_settings()
    token = settings.apify_api_token.get_secret_value() if settings.apify_api_token else ""
    if not token:
        print("APIFY_API_TOKEN is not set in .env — cannot probe Apify.")
        return 1

    body = {"directUrls": [url], "resultsType": "details", "resultsLimit": 5}
    print(f"[apify] running {ACTOR} on {url} ... (can take 20-90s)")
    try:
        response = httpx.post(
            ENDPOINT,
            # Header auth (as the provider does) keeps the token out of logged request URLs.
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=settings.apify_timeout_seconds + 60,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"request failed: {exc}")
        return 1

    if response.status_code >= 400:
        print(f"HTTP {response.status_code}: {response.text[:600]}")
        return 1

    items = response.json()
    print(f"[apify] returned {len(items)} dataset item(s)")
    if not items:
        print("no data — profile may be private, age-gated, or blocked.")
        return 0

    first = items[0]
    print("\ntop-level keys:")
    print(sorted(first.keys()))

    print("\nprofile fields of interest:")
    profile = {k: first.get(k) for k in PROFILE_FIELDS if k in first}
    print(json.dumps(profile, indent=2, default=str))

    posts = first.get("latestPosts") or first.get("posts") or []
    if isinstance(posts, list) and posts:
        print(f"\nlatestPosts: {len(posts)} item(s); first-post engagement fields:")
        sample = posts[0]
        engagement = {k: sample.get(k) for k in POST_ENGAGEMENT_FIELDS if k in sample}
        print(json.dumps(engagement, indent=2, default=str))
        print("first-post all keys:", sorted(sample.keys()))
    else:
        print("\nno latestPosts in the details payload (may need resultsType='posts').")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
