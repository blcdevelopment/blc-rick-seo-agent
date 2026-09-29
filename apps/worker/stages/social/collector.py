"""Social audit collector — orchestrates provider fetch -> extractor.

Graceful by design (matches the PSI / external-SEO pattern): no handles, or no usable provider
credential for any requested platform => ``skipped`` (never penalizes / aborts); per-platform
fetch failures degrade to ``partial``/``failed`` via the extractor. Dispatch is generic over the
:mod:`providers` registry (Instagram/Facebook via Apify, YouTube via the YouTube Data API), so the
collector never names a platform — adding a backend touches only ``providers``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.shared.config import Settings
from apps.worker.stages.social.extractor import extract_social_facts
from apps.worker.stages.social.providers import SocialProvider, get_provider

JsonDict = dict[str, Any]


def _skipped(reason: str) -> JsonDict:
    return {
        "status": "skipped",
        "reason": reason,
        "source": "social",
        "summary": {},
        "platforms": [],
    }


def _missing_credential_reason(active_platforms: set[str]) -> str:
    """Skip reason when no requested platform has a usable credential.

    Only Apify-backed platforms (Instagram/Facebook) and YouTube exist; a YouTube-only request
    is missing its API key, anything else is missing the Apify token.
    """
    if active_platforms <= {"youtube"}:
        return "missing_youtube_api_key"
    return "missing_apify_api_token"


def collect_social_facts(settings: Settings, handles: dict[str, str | None] | None) -> JsonDict:
    active = {platform: handle for platform, handle in (handles or {}).items() if handle}
    if not active:
        return _skipped("no_social_handles")

    providers: dict[str, SocialProvider | None] = {p: get_provider(p) for p in active}

    # Skip early only when we cannot fetch ANY requested platform (an unsupported platform or a
    # per-platform missing key just yields a None fetch -> failed, handled by the extractor).
    usable = any(
        provider is not None and provider.credential_available(settings)
        for provider in providers.values()
    )
    if not usable:
        return _skipped(_missing_credential_reason(set(active)))

    fetched: list[JsonDict] = []
    for platform, handle in active.items():
        provider = providers[platform]
        raw = provider.fetch(handle, settings) if provider is not None else None
        fetched.append({"platform": platform, "handle": handle, "raw": raw})

    return extract_social_facts(fetched, now=datetime.now(UTC))
