"""Social data provider adapter — a uniform interface + registry (P2-19 / SMWA-71).

Each platform backend is a :class:`SocialProvider`: it declares its ``platform`` key, whether
its credential is configured (``credential_available``), and how to ``fetch`` the raw public
payload (or ``None`` so the collector degrades gracefully — the missing-key pattern shared with
PSI / external-SEO). The :data:`registry` lets ``collector`` dispatch over platforms generically
instead of a hardcoded ``if/elif``, so adding a backend (e.g. TikTok) is one class + one registry
entry with no collector change.

The low-level network calls stay in ``apify_provider`` / ``youtube_provider`` (unit-tested in
isolation); the providers here are thin, dependency-light adapters over them.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from apps.shared.config import Settings
from apps.worker.stages.social.apify_provider import (
    fetch_facebook_page,
    fetch_facebook_posts,
    fetch_instagram_profile,
)
from apps.worker.stages.social.youtube_provider import fetch_youtube_channel

JsonDict = dict[str, Any]


@runtime_checkable
class SocialProvider(Protocol):
    """Uniform contract every social backend implements."""

    #: Platform key matching ``audit_jobs.social_handles`` and the extractor's normalizers.
    platform: str

    def credential_available(self, settings: Settings) -> bool:
        """True when the provider's credential is configured (else it cannot fetch)."""
        ...

    def fetch(self, handle: str, settings: Settings) -> JsonDict | None:
        """Fetch the raw public payload for ``handle``; ``None`` on missing key/failure."""
        ...


def _apify_token(settings: Settings) -> str:
    return settings.apify_api_token.get_secret_value() if settings.apify_api_token else ""


def _youtube_key(settings: Settings) -> str:
    return settings.youtube_api_key.get_secret_value() if settings.youtube_api_key else ""


class InstagramProvider:
    """Public Instagram profiles via the Apify Instagram Scraper actor."""

    platform = "instagram"

    def credential_available(self, settings: Settings) -> bool:
        return bool(_apify_token(settings))

    def fetch(self, handle: str, settings: Settings) -> JsonDict | None:
        return fetch_instagram_profile(handle, settings)


class FacebookProvider:
    """Public Facebook pages via the Apify Pages actor, enriched with the Posts actor.

    The Pages actor returns page metadata only; this provider also pulls the page's posts so
    the extractor can derive FB cadence/recency/engagement (graceful: a page with no posts
    still scores — the dependent rules ``skip_if_missing``).
    """

    platform = "facebook"

    def credential_available(self, settings: Settings) -> bool:
        return bool(_apify_token(settings))

    def fetch(self, handle: str, settings: Settings) -> JsonDict | None:
        raw = fetch_facebook_page(handle, settings)
        if raw is None:
            return None
        posts = fetch_facebook_posts(handle, settings)
        if posts:
            raw = {**raw, "posts": posts}
        return raw


class YouTubeProvider:
    """Public YouTube channels via the free YouTube Data API v3 (no OAuth)."""

    platform = "youtube"

    def credential_available(self, settings: Settings) -> bool:
        return bool(_youtube_key(settings))

    def fetch(self, handle: str, settings: Settings) -> JsonDict | None:
        return fetch_youtube_channel(handle, settings)


#: The single source of truth mapping a platform key to its provider.
registry: dict[str, SocialProvider] = {
    provider.platform: provider
    for provider in (InstagramProvider(), FacebookProvider(), YouTubeProvider())
}


def get_provider(platform: str) -> SocialProvider | None:
    """Return the provider for ``platform`` (``None`` for an unsupported platform)."""
    return registry.get(platform)


def supported_platforms() -> tuple[str, ...]:
    """The platform keys the social audit can currently fetch."""
    return tuple(registry)
