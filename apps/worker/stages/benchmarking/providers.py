"""Competitor-benchmarking provider adapter — a uniform interface + registry (P2-26 / SMWA-79).

Each vendor backend is a :class:`BenchmarkProvider`: it declares its ``name``, whether its
credential is configured (``credential_available``), and how to ``fetch`` competitor/industry
baseline data (or ``None`` so the collector degrades gracefully — the missing-key pattern shared
with the social providers / PSI / GSC). The :data:`registry` lets ``collector`` dispatch generically
so adding a vendor is one class + one registry entry.

DEFERRED (v3): the low-level paid-vendor HTTP clients are **not implemented** — there is no free,
reliable competitor-benchmark source, so a live client requires a selected vendor + an approved
recurring cost (the ticket's acceptance gate). Until then every provider's ``fetch`` is a documented
no-op returning ``None``: nothing is fabricated and no cost is incurred. When a vendor is chosen,
implement its ``fetch`` (returning the ``{"competitors": [...]}`` shape ``collector.normalize``
expects) and nothing else in the seam changes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from apps.shared.config import Settings

JsonDict = dict[str, Any]


@runtime_checkable
class BenchmarkProvider(Protocol):
    """Uniform contract every benchmarking backend implements."""

    #: Vendor key matching ``settings.benchmark_provider`` (e.g. ``"semrush"``).
    name: str

    def credential_available(self, settings: Settings) -> bool:
        """True when this vendor is selected AND its API key is configured."""
        ...

    def fetch(
        self,
        *,
        target_url: str,
        niche: str | None,
        competitors: list[str],
        settings: Settings,
    ) -> JsonDict | None:
        """Fetch baseline data for the target/competitors; ``None`` on missing key/failure/no-op."""
        ...


def _api_key(settings: Settings) -> str:
    return settings.benchmark_api_key.get_secret_value() if settings.benchmark_api_key else ""


class _PaidVendorProvider:
    """Base for the deferred paid vendors (SEMrush / Ahrefs / Similarweb).

    ``credential_available`` is honest — it is only True when the operator has both *selected* this
    vendor (``benchmark_provider``) and supplied an API key. ``fetch`` is a deliberate no-op until
    the live client is implemented (see module docstring), so enabling benchmarking with a key still
    degrades to a graceful ``skipped`` rather than fabricating baselines.
    """

    name: str = ""

    def credential_available(self, settings: Settings) -> bool:
        selected = (settings.benchmark_provider or "").strip().lower()
        return selected == self.name and bool(_api_key(settings))

    def fetch(
        self,
        *,
        target_url: str,
        niche: str | None,
        competitors: list[str],
        settings: Settings,
    ) -> JsonDict | None:
        # TODO(P2-26): implement the live vendor client once a vendor is selected and its
        # recurring cost is approved. Return the shape collector.normalize_benchmark_facts expects:
        # {"competitors": [{"label": ..., "seo": .., "uxui": .., ...}, ...]}. Deliberate no-op
        # until then — nothing fabricated, no cost incurred.
        return None


class SemrushProvider(_PaidVendorProvider):
    name = "semrush"


class AhrefsProvider(_PaidVendorProvider):
    name = "ahrefs"


class SimilarwebProvider(_PaidVendorProvider):
    name = "similarweb"


#: The single source of truth mapping a vendor key to its provider.
registry: dict[str, BenchmarkProvider] = {
    provider.name: provider
    for provider in (SemrushProvider(), AhrefsProvider(), SimilarwebProvider())
}


def get_provider(name: str) -> BenchmarkProvider | None:
    """Return the provider for vendor ``name`` (``None`` for unsupported/unset)."""
    return registry.get(name)


def supported_providers() -> tuple[str, ...]:
    """The vendor keys the benchmarking layer knows about."""
    return tuple(registry)
