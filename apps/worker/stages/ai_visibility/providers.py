"""AI Visibility provider adapter — a uniform interface + registry.

Each backend is an :class:`AiVisibilityProvider`: it declares its ``name``, whether its credentials
are configured (``credential_available``), and how to ``fetch`` a raw extraction for a domain (or
``None`` so the collector degrades gracefully — the missing-key pattern shared with the social /
benchmarking / PSI collectors). The :data:`registry` lets ``collector`` dispatch generically, so
adding a backend (e.g. a CSV-import provider, or another AI-visibility vendor) is one class + one
registry entry with no collector change.

Today there is one provider: :class:`SemrushProvider`, which drives the Playwright login-bot in
:mod:`semrush_scraper`. Its credentials are "configured" when EITHER a saved browser session exists
OR an email+password is set (the bot establishes/refreshes the session); OpenAI must also be
configured because extraction is vision-based.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from apps.shared.config import Settings

JsonDict = dict[str, Any]


@runtime_checkable
class AiVisibilityProvider(Protocol):
    """Uniform contract every AI-visibility backend implements."""

    #: Provider key matching ``settings.ai_visibility_provider`` (e.g. ``"semrush"``).
    name: str

    def credential_available(self, settings: Settings) -> bool:
        """True when this provider has enough configured to attempt a fetch."""
        ...

    def fetch(self, *, domain: str, settings: Settings) -> JsonDict | None:
        """Fetch a raw extraction for ``domain``; ``None`` on missing creds/failure."""
        ...


def _openai_configured(settings: Settings) -> bool:
    return bool(settings.openai_api_key and settings.openai_api_key.get_secret_value())


class SemrushProvider:
    """Semrush AI Visibility Toolkit via the Playwright login-bot (see :mod:`semrush_scraper`)."""

    name = "semrush"

    def credential_available(self, settings: Settings) -> bool:
        # Extraction is vision-based, so OpenAI is required regardless of how we authenticate.
        if not _openai_configured(settings):
            return False
        session_path = (settings.semrush_session_state_path or "").strip()
        has_session = bool(session_path) and Path(session_path).is_file()
        # A configured email signals the operator intends to use Semrush — enough to proceed so the
        # bot can render the "connect Semrush" note when no session exists yet (it never types the
        # password itself unless semrush_allow_headless_login is on). A saved session alone also
        # qualifies (no email needed once connected).
        has_intent = bool((settings.semrush_email or "").strip())
        return has_session or has_intent

    def fetch(self, *, domain: str, settings: Settings) -> JsonDict | None:
        # Imported lazily so the (Playwright-dependent) bot is only loaded when actually used —
        # the collector's skip paths and the pure normalizer/report builder never import it.
        from apps.worker.stages.ai_visibility.semrush_scraper import (
            fetch_semrush_ai_visibility_sync,
        )

        return fetch_semrush_ai_visibility_sync(domain=domain, settings=settings)


#: The single source of truth mapping a provider key to its implementation.
registry: dict[str, AiVisibilityProvider] = {
    provider.name: provider for provider in (SemrushProvider(),)
}


def get_provider(name: str) -> AiVisibilityProvider | None:
    """Return the provider for ``name`` (``None`` for unsupported/unset)."""
    return registry.get(name)


def supported_providers() -> tuple[str, ...]:
    """The provider keys the AI-visibility layer knows about."""
    return tuple(registry)
