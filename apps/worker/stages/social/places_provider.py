"""Google Places API (New) backend — public Google Business Profile data (SAE-12).

Pulls a business's public Google listing (address, phone, category, rating, review count,
website) for the combined audit's business-identity enrichment. PUBLIC data: a plain API key,
no owner consent (contrast the owner-only Google Business Profile API). Two calls, per Google's
model: Text Search (New) resolves a query to a place id, then Place Details (New) returns the
fields. Network-only fetchers return the raw payload or ``None`` so the collector degrades
gracefully (the missing-key pattern shared with Apify / PSI); the token is read from Settings
and never logged. ``normalize_google_business`` is pure and unit-testable from a fixture.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx
from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings
from apps.worker.stages.social.extractor import _clean
from apps.worker.stages.technical_crawl_common import (
    GENERIC_SECOND_LEVELS,
    MULTI_TENANT_PLATFORMS,
    PATH_TENANT_HOSTS,
    registrable_brand_label,
)

JsonDict = dict[str, Any]

_SEARCH_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
_DETAILS_ENDPOINT = "https://places.googleapis.com/v1/places"
# Fields we bill for on the Details call (Enterprise SKU); a minimal mask keeps cost down.
_DETAILS_FIELD_MASK = ",".join(
    (
        "displayName",
        "formattedAddress",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "types",
        "primaryTypeDisplayName",
        "rating",
        "userRatingCount",
        "websiteUri",
        "businessStatus",
    )
)


def _api_key(settings: Settings) -> str:
    key = settings.google_places_api_key
    return key.get_secret_value() if key else ""


def _text(value: Any) -> str:
    """A Places display field is often ``{"text": "...", "languageCode": "en"}`` — flatten it."""
    if isinstance(value, dict):
        value = value.get("text")
    return _clean(value)


# Shared hosts where the TENANT is a path, not a subdomain (technical_crawl_common — the one
# home, shared with registrable_brand_label): same host is NOT the same business there, so the
# listing's website must point into the audited site's path.
_PATH_TENANT_HOSTS = PATH_TENANT_HOSTS


def _clean_host(url_parts: Any) -> str:
    return (url_parts.hostname or "").lower().rstrip(".").removeprefix("www.")


def _website_matches(business_website: Any, expected_url: str) -> bool:
    """True when the listing's website is the audited business's site.

    Accepted: the same host (minus ``www.``), or a subdomain-vs-apex relationship
    (``shop.acme.com`` <-> ``acme.com``) — EXCEPT when the shared apex is a known
    multi-tenant platform (``foo.wixsite.com`` vs ``wixsite.com``), where a suffix rule
    would attribute a stranger's, or the platform's own, listing to the client. On
    path-tenant hosts (``sites.google.com/view/<tenant>``) the listing must additionally
    point into the audited site's path. A listing with no website never matches:
    better no Google data (the rules skip-rescale) than a stranger's reviews/phone in a
    client-facing report."""
    listing = urlsplit(str(business_website or ""))
    audited = urlsplit(expected_url)
    listing_host = _clean_host(listing)
    audited_host = _clean_host(audited)
    if not listing_host or not audited_host:
        return False
    if listing_host == audited_host or listing_host.endswith("." + audited_host):
        shared_apex = audited_host
    elif audited_host.endswith("." + listing_host):
        shared_apex = listing_host
    else:
        return False
    if listing_host != audited_host:
        # The shared apex must carry a real BRAND label. A single-label apex ("com"), a bare
        # public-suffix family ("co.uk"), or a multi-tenant platform apex ("wixsite.com") can
        # never establish that the listing and the audited site are the same business — while
        # a genuine ccTLD apex like acme.co.uk still passes (its brand label is "acme").
        apex_brand = registrable_brand_label(shared_apex)
        if not apex_brand or apex_brand in (MULTI_TENANT_PLATFORMS | GENERIC_SECOND_LEVELS):
            return False
    if audited_host in _PATH_TENANT_HOSTS:
        audited_path = audited.path.strip("/")
        listing_path = listing.path.strip("/")
        if audited_path:
            return listing_path == audited_path or listing_path.startswith(audited_path + "/")
    return True


def fetch_place_id(query: str, settings: Settings) -> str | None:
    """Resolve a business name / website query to a Places place id (Text Search, New)."""
    key = _api_key(settings)
    if not key or not query.strip():
        return None
    try:
        response = httpx.post(
            _SEARCH_ENDPOINT,
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.id"},
            json={"textQuery": query.strip()},
            timeout=settings.google_places_timeout_seconds,
        )
        if response.status_code >= 400:
            return None
        places = response.json().get("places")
    except SoftTimeLimitExceeded:
        # The worker is out of time: propagate so the task can mark the job failed honestly
        # instead of the hard limit killing it mid-pipeline (crawler/site_health convention).
        raise
    except Exception:
        return None
    if not isinstance(places, list) or not places:
        return None
    first = places[0]
    place_id = first.get("id") if isinstance(first, dict) else None
    return place_id if isinstance(place_id, str) and place_id else None


def fetch_place_details(place_id: str, settings: Settings) -> JsonDict | None:
    """Fetch Place Details (New) for a place id with the minimal billed field mask."""
    key = _api_key(settings)
    if not key or not place_id:
        return None
    try:
        response = httpx.get(
            f"{_DETAILS_ENDPOINT}/{place_id}",
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": _DETAILS_FIELD_MASK},
            timeout=settings.google_places_timeout_seconds,
        )
        if response.status_code >= 400:
            return None
        payload = response.json()
    except SoftTimeLimitExceeded:
        # The worker is out of time: propagate so the task can mark the job failed honestly
        # instead of the hard limit killing it mid-pipeline (crawler/site_health convention).
        raise
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def normalize_google_business(raw: JsonDict) -> JsonDict:
    """Pure normalization of a Place Details payload into flat GBP facts (deterministic)."""
    phone = _text(raw.get("nationalPhoneNumber")) or _text(raw.get("internationalPhoneNumber"))
    types = raw.get("types")
    types = [t for t in types if isinstance(t, str)] if isinstance(types, list) else []
    rating = raw.get("rating")
    review_count = raw.get("userRatingCount")
    return {
        "name": _text(raw.get("displayName")) or None,
        "address": _text(raw.get("formattedAddress")) or None,
        "phone": phone or None,
        "category": _text(raw.get("primaryTypeDisplayName")) or (types[0] if types else None),
        "types": types,
        "rating": float(rating) if isinstance(rating, (int, float)) else None,
        "review_count": int(review_count) if isinstance(review_count, (int, float)) else None,
        "website": _text(raw.get("websiteUri")) or None,
        "business_status": _text(raw.get("businessStatus")) or None,
    }


def collect_google_business_facts(
    settings: Settings, *, query: str, expected_url: str | None = None
) -> JsonDict:
    """Resolve + fetch + normalize a business's public Google listing.

    Graceful at every not-ready state (mirrors the social/benchmark collectors): no key or no
    query => ``skipped``; a failed lookup => ``failed``; success => ``complete`` with ``business``.
    When ``expected_url`` is given, the matched listing's website must belong to that site —
    Text Search is a fuzzy name lookup, and without this gate the first hit for a generic domain
    label can be a different business entirely, whose reviews/phone would then be scored and
    NAP-checked as the client's. A mismatch (or a listing with no website) => ``failed`` with
    reason ``website_mismatch``, which the caller treats like any other miss (skip + rescale).
    Never raises; a missing/failed Google listing leaves the combined report unchanged.
    """
    if not _api_key(settings):
        return {
            "status": "skipped",
            "reason": "missing_google_places_api_key",
            "source": "google_business",
        }
    if not query or not query.strip():
        return {"status": "skipped", "reason": "no_business_query", "source": "google_business"}
    place_id = fetch_place_id(query, settings)
    if not place_id:
        return {"status": "failed", "reason": "place_not_found", "source": "google_business"}
    raw = fetch_place_details(place_id, settings)
    if raw is None:
        return {"status": "failed", "reason": "details_unavailable", "source": "google_business"}
    business = normalize_google_business(raw)
    if expected_url is not None and not _website_matches(business.get("website"), expected_url):
        return {"status": "failed", "reason": "website_mismatch", "source": "google_business"}
    return {
        "status": "complete",
        "source": "google_business",
        "business": business,
    }
