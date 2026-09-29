from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.shared.config import Settings
from apps.shared.models import GoogleSearchConsoleConnection

# Shared with the social extractor so "per month" means the same thing for search figures
# and social cadence inside one report.
from apps.worker.stages.social.extractor import AVG_DAYS_PER_MONTH as _AVG_DAYS_PER_MONTH
from apps.worker.stages.technical_crawl_common import registrable_brand_label

JsonDict = dict[str, Any]

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
SITES_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites"
URL_INSPECTION_ENDPOINT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
GSC_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/webmasters.readonly",
)
# Owner-only YouTube Analytics scopes, appended to the Google consent only when connected-mode
# YouTube is enabled (SAE-15). Same Google OAuth app + token store — no migration.
YOUTUBE_ANALYTICS_SCOPES = (
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
)


def oauth_scopes(settings: Settings) -> tuple[str, ...]:
    """The Google OAuth scopes to request: Search Console always, plus YouTube Analytics when
    connected-mode YouTube is enabled (so one consent grants both)."""
    if getattr(settings, "youtube_analytics_connect_enabled", False):
        return GSC_SCOPES + YOUTUBE_ANALYTICS_SCOPES
    return GSC_SCOPES


def build_google_oauth_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(oauth_scopes(settings)),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def google_oauth_configured(settings: Settings) -> bool:
    return bool(
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.google_oauth_client_secret.get_secret_value()
    )


def exchange_google_oauth_code(code: str, settings: Settings) -> JsonDict:
    secret = settings.google_oauth_client_secret.get_secret_value()
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": secret,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def refresh_google_access_token(
    connection: GoogleSearchConsoleConnection,
    settings: Settings,
    db: Session,
) -> str:
    if not connection.refresh_token:
        raise ValueError("Google connection does not have a refresh token.")
    secret = settings.google_oauth_client_secret.get_secret_value()
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": settings.google_oauth_client_id,
            "client_secret": secret,
            "refresh_token": connection.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    token_payload = response.json()
    access_token = str(token_payload["access_token"])
    connection.access_token = access_token
    connection.token_expires_at = _expires_at(token_payload)
    db.commit()
    db.refresh(connection)
    return access_token


def ensure_google_access_token(
    connection: GoogleSearchConsoleConnection,
    settings: Settings,
    db: Session,
) -> str:
    expires_at = connection.token_expires_at
    # SQLite (QA harness/tests) returns naive datetimes; Postgres returns aware ones.
    if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        connection.access_token
        and isinstance(expires_at, datetime)
        and expires_at > datetime.now(UTC) + timedelta(seconds=60)
    ):
        return connection.access_token
    return refresh_google_access_token(connection, settings, db)


def google_userinfo(access_token: str) -> JsonDict:
    response = httpx.get(
        USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def list_search_console_sites(access_token: str) -> list[JsonDict]:
    response = httpx.get(
        SITES_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return [
        {"siteUrl": str(item.get("siteUrl")), "permissionLevel": item.get("permissionLevel")}
        for item in response.json().get("siteEntry", [])
        if item.get("siteUrl")
    ]


def _granted_scopes(token_payload: JsonDict) -> list[str]:
    """The scopes Google actually granted (space-delimited ``scope`` in the token response) —
    stored so the connection metadata reflects the real grant, not just the GSC baseline.
    With connected-YouTube enabled the consent may include the Analytics scope (or the user
    may untick one); a payload without ``scope`` falls back to the GSC set."""
    granted = str(token_payload.get("scope") or "").split()
    return granted or list(GSC_SCOPES)


def upsert_google_connection(
    db: Session,
    *,
    account_email: str,
    token_payload: JsonDict,
    properties: list[JsonDict],
) -> GoogleSearchConsoleConnection:
    connection = db.scalar(
        select(GoogleSearchConsoleConnection).where(
            GoogleSearchConsoleConnection.account_email == account_email
        )
    )
    if connection is None:
        connection = GoogleSearchConsoleConnection(
            account_email=account_email,
            scopes={"values": _granted_scopes(token_payload)},
            properties={"siteEntry": properties},
        )
        db.add(connection)

    connection.access_token = str(token_payload.get("access_token") or "")
    refresh_token = token_payload.get("refresh_token")
    if refresh_token:
        connection.refresh_token = str(refresh_token)
    connection.token_expires_at = _expires_at(token_payload)
    connection.scopes = {"values": _granted_scopes(token_payload)}
    connection.properties = {"siteEntry": properties}
    db.commit()
    db.refresh(connection)
    return connection


def latest_google_connection(db: Session) -> GoogleSearchConsoleConnection | None:
    return db.scalar(
        select(GoogleSearchConsoleConnection).order_by(
            GoogleSearchConsoleConnection.created_at.desc()
        )
    )


def collect_google_search_console_facts(
    *,
    url: str,
    page_urls: list[str],
    settings: Settings,
    db: Session,
) -> JsonDict:
    started_at = _utc_now()
    if not google_oauth_configured(settings):
        return _external_google_payload(
            gsc=_skipped("oauth_not_configured", started_at),
            url_inspection=_skipped("oauth_not_configured", started_at),
        )

    connection = latest_google_connection(db)
    if connection is None:
        return _external_google_payload(
            gsc=_skipped("no_google_connection", started_at),
            url_inspection=_skipped("no_google_connection", started_at),
        )

    try:
        access_token = ensure_google_access_token(connection, settings, db)
        properties = _connection_properties(connection)
        if not properties:
            properties = list_search_console_sites(access_token)
            connection.properties = {"siteEntry": properties}
            db.commit()
            db.refresh(connection)
        matched_property = match_search_console_property(url, properties)
        if matched_property is None:
            return _external_google_payload(
                gsc=_skipped("no_matching_search_console_property", started_at),
                url_inspection=_skipped("no_matching_search_console_property", started_at),
            )

        gsc = collect_search_analytics(
            access_token=access_token,
            site_url=str(matched_property["siteUrl"]),
            settings=settings,
            started_at=started_at,
        )
        inspected = collect_url_inspection(
            access_token=access_token,
            site_url=str(matched_property["siteUrl"]),
            urls=_priority_urls(url, page_urls, settings.url_inspection_max_urls),
            started_at=started_at,
        )
        return _external_google_payload(gsc=gsc, url_inspection=inspected)
    except SoftTimeLimitExceeded:
        # The whole Celery task is out of budget; fail fast instead of recording
        # a "failed enrichment" and letting the hard limit kill the job mid-write.
        raise
    except Exception as exc:  # noqa: BLE001 - enrichment must not fail the audit
        return _external_google_payload(
            gsc=_failed(str(exc), started_at),
            url_inspection=_failed(str(exc), started_at),
        )


def collect_search_analytics(
    *,
    access_token: str,
    site_url: str,
    settings: Settings,
    started_at: str,
) -> JsonDict:
    end_date = date.today() - timedelta(days=3)
    start_date = end_date - timedelta(days=settings.gsc_default_date_range_days)
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=settings.gsc_default_date_range_days)

    top_queries = query_search_analytics(
        access_token,
        site_url,
        start_date=start_date,
        end_date=end_date,
        dimensions=["query"],
        row_limit=min(settings.gsc_row_limit, 25000),
    )
    top_pages = query_search_analytics(
        access_token,
        site_url,
        start_date=start_date,
        end_date=end_date,
        dimensions=["page"],
        row_limit=min(settings.gsc_row_limit, 25000),
    )
    previous_pages = query_search_analytics(
        access_token,
        site_url,
        start_date=previous_start,
        end_date=previous_end,
        dimensions=["page"],
        row_limit=min(settings.gsc_row_limit, 25000),
    )

    opportunities = _ranking_opportunities(top_queries)
    low_ctr_pages = _low_ctr_pages(top_pages)
    declining_pages = _declining_pages(top_pages, previous_pages)
    brand_token = _brand_token(site_url)
    window_days = (end_date - start_date).days + 1
    # Site totals from the page dimension, computed BEFORE truncation — used to cap the
    # opportunity model and to let prose reconcile against the site's real click volume.
    site_total_clicks = sum(float(row.get("clicks") or 0) for row in top_pages)
    site_total_impressions = sum(float(row.get("impressions") or 0) for row in top_pages)
    return {
        "status": "complete",
        "source": "search_console_api",
        "site_url": site_url,
        "date_range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": window_days,
        },
        "previous_date_range": {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
            "days": window_days,
        },
        "summary": {
            "top_query_count": len(top_queries),
            "top_page_count": len(top_pages),
            "ranking_opportunities": len(opportunities),
            "high_impression_low_ctr_pages": len(low_ctr_pages),
            "declining_pages": len(declining_pages),
            "total_clicks": int(site_total_clicks + 0.5),
            "total_impressions": int(site_total_impressions + 0.5),
        },
        "top_queries": top_queries[:50],
        "top_pages": top_pages[:50],
        "ranking_opportunities": opportunities[:25],
        "high_impression_low_ctr_pages": low_ctr_pages[:25],
        "declining_pages": declining_pages[:25],
        # Business-opportunity framing (P1-P4); see helpers above. Stored as facts so the grounding
        # validator keeps any executive-summary prose that cites these numbers.
        "opportunity": _opportunity_estimate(
            opportunities,
            window_days=window_days,
            site_total_clicks=site_total_clicks,
            aio_prevalence=settings.gsc_opportunity_aio_prevalence,
            cap_multiple=settings.gsc_opportunity_cap_multiple,
        ),
        "branded": _branded_split(top_queries, site_url),
        "topic_clusters": _topic_clusters(top_queries, brand_token)[:8],
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def query_search_analytics(
    access_token: str,
    site_url: str,
    *,
    start_date: date,
    end_date: date,
    dimensions: list[str],
    row_limit: int,
) -> list[JsonDict]:
    encoded_site = quote(site_url, safe="")
    response = httpx.post(
        f"https://www.googleapis.com/webmasters/v3/sites/{encoded_site}/searchAnalytics/query",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "dimensions": dimensions,
            "type": "web",
            "rowLimit": row_limit,
        },
        timeout=45,
    )
    response.raise_for_status()
    return [_normalize_search_row(row, dimensions) for row in response.json().get("rows", [])]


def collect_url_inspection(
    *,
    access_token: str,
    site_url: str,
    urls: list[str],
    started_at: str,
) -> JsonDict:
    if not urls:
        return _skipped("no_urls_to_inspect", started_at)

    inspections = []
    for url in urls:
        try:
            response = httpx.post(
                URL_INSPECTION_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"inspectionUrl": url, "siteUrl": site_url},
                timeout=45,
            )
            if response.status_code >= 400:
                inspections.append({"url": url, "status": "failed", "error": response.text[:300]})
                continue
            inspections.append(_normalize_inspection(url, response.json()))
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - per-URL failure is data, not task failure
            inspections.append({"url": url, "status": "failed", "error": str(exc)[:300]})

    not_on_google = sum(1 for item in inspections if item.get("on_google") is False)
    failed = sum(1 for item in inspections if item.get("status") == "failed")
    succeeded = len(inspections) - failed
    # Honest status: a run where some/all per-URL inspections errored must not
    # present itself as a clean "complete" — the not_on_google count would be an
    # undercount, and the scoring layer only trusts complete sources.
    if succeeded == 0:
        status = "failed"
    elif failed > 0:
        status = "partial"
    else:
        status = "complete"
    return {
        "status": status,
        "source": "url_inspection_api",
        "site_url": site_url,
        "summary": {
            "urls_requested": len(urls),
            "urls_inspected": succeeded,
            "not_on_google": not_on_google,
            "failed": failed,
        },
        "items": inspections,
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def match_search_console_property(url: str, properties: list[JsonDict]) -> JsonDict | None:
    normalized_url = _normalized_url_for_match(url)
    parsed = urlparse(normalized_url)
    host = (parsed.hostname or "").lower().removeprefix("www.")

    url_prefix_matches = [
        item
        for item in properties
        if str(item.get("siteUrl") or "").startswith(("http://", "https://"))
        and normalized_url.startswith(str(item.get("siteUrl")))
    ]
    if url_prefix_matches:
        return max(url_prefix_matches, key=lambda item: len(str(item["siteUrl"])))

    # Next preference: a domain property. It aggregates every scheme/subdomain,
    # so it beats a scheme- or www-mismatched url-prefix property that may hold
    # only a stale slice of the site's data.
    domain_matches = []
    for item in properties:
        site_url = str(item.get("siteUrl") or "")
        if not site_url.startswith("sc-domain:"):
            continue
        domain = site_url.removeprefix("sc-domain:").lower().removeprefix("www.")
        if host == domain or host.endswith(f".{domain}"):
            domain_matches.append(item)
    if domain_matches:
        return domain_matches[0]

    # Last resort: relaxed url-prefix match tolerating www./scheme variants so
    # auditing https://www.example.com still finds the https://example.com/
    # property. Prefer https, then the longest (most specific) path prefix.
    relaxed_matches = []
    for item in properties:
        site_url = str(item.get("siteUrl") or "")
        if not site_url.startswith(("http://", "https://")):
            continue
        site_parsed = urlparse(site_url)
        site_host = (site_parsed.hostname or "").lower().removeprefix("www.")
        site_path = site_parsed.path or "/"
        if site_host == host and (parsed.path or "/").startswith(site_path):
            relaxed_matches.append((site_parsed.scheme == "https", len(site_path), item))
    if relaxed_matches:
        return max(relaxed_matches, key=lambda entry: (entry[0], entry[1]))[2]
    return None


def _normalize_search_row(row: JsonDict, dimensions: list[str]) -> JsonDict:
    keys = row.get("keys") if isinstance(row.get("keys"), list) else []
    values = {
        dimension: keys[index] for index, dimension in enumerate(dimensions) if index < len(keys)
    }
    return {
        **values,
        "clicks": round(float(row.get("clicks") or 0), 2),
        "impressions": round(float(row.get("impressions") or 0), 2),
        "ctr": round(float(row.get("ctr") or 0), 4),
        "position": round(float(row.get("position") or 0), 2),
    }


def _ranking_opportunities(rows: list[JsonDict]) -> list[JsonDict]:
    return [
        row
        for row in rows
        if float(row.get("impressions") or 0) >= 50
        and _STRIKING_POSITION_MIN <= float(row.get("position") or 0) <= _STRIKING_POSITION_MAX
    ]


def _low_ctr_pages(rows: list[JsonDict]) -> list[JsonDict]:
    return [
        row
        for row in rows
        if float(row.get("impressions") or 0) >= 100 and float(row.get("ctr") or 0) <= 0.02
    ]


# --- Business-opportunity framing (P1-P4) ------------------------------------------------------
# These helpers translate the site's OWN Search Console data into business-shaped, leading-indicator
# facts (clicks/leads left on the table, branded demand, topic-cluster visibility). Every number is
# a transparent function of real GSC impressions/CTR + published benchmarks, stored as a FACT so the
# grounding validator keeps any prose that cites it. They are estimates/ranges, never guarantees,
# and never a revenue/CAC figure (out of scope without CRM data). Half-up rounding (int(x + 0.5))
# matches the project convention.

# Blended organic CTR-by-position curve, deliberately conservative: the position-1 value
# sits in the 20-28% band reported by the large GSC-derived studies (Backlinko, SISTRIX,
# seoClarity). The First Page Sage meta-analysis (P1 = 39.8%) is the optimistic outlier
# and is no longer the default. Versioned like a rubric so stored estimates stay
# explainable; positions past 10 fall back to the position-10 CTR.
_CTR_CURVE_VERSION = "blended-conservative-v1"
_CTR_CURVE: dict[int, float] = {
    1: 0.276,
    2: 0.157,
    3: 0.110,
    4: 0.080,
    5: 0.061,
    6: 0.047,
    7: 0.038,
    8: 0.031,
    9: 0.026,
    10: 0.022,
}
_CTR_CURVE_SOURCE = (
    "blended average of GSC-derived organic CTR studies (Backlinko / SISTRIX / seoClarity)"
)
# Conservative target band: model near-miss queries reaching the top of page 1 (position 5 =
# low end, position 3 = high end). Never position 1 — that would overstate the opportunity.
_OPPORTUNITY_TARGET_LOW, _OPPORTUNITY_TARGET_HIGH = 5, 3
# The striking-distance ("near-miss") definition, stored as facts so grounded prose can
# state it: queries already ranking just below the top results.
_STRIKING_POSITION_MIN, _STRIKING_POSITION_MAX = 4, 20
# Model only the highest-impression striking queries — projecting every ranking query
# moving at once is not defensible.
_OPPORTUNITY_MAX_QUERIES = 25
# AI Overviews suppress organic CTR MOST at the top of the SERP and less further down, so the
# modeled upside gets a POSITION-AWARE haircut, not a flat one. Per-rank CTR reduction (the CTR
# loss on queries where an AI Overview is present) is from Ahrefs' Dec-2025 study of ~300k
# keywords (https://ahrefs.com/blog/ai-overviews-reduce-clicks-update/): -58% at rank 1 decaying
# to -19.4% at rank 10. Positions 6-9 are linearly interpolated between the published pos-5
# (32.6%) and pos-10 (19.4%) points; past 10 falls back to the pos-10 value. The discount applied
# at a target rank is prevalence * reduction-at-that-rank, so a query modeled reaching position 3
# is discounted more than one reaching position 5 (the earlier code applied a single flat 15%,
# which understated the top-of-page suppression the modeled targets sit in).
_AIO_MODEL_VERSION = "position-aware-v1"
_AIO_CTR_REDUCTION: dict[int, float] = {
    1: 0.580,
    2: 0.508,
    3: 0.464,
    4: 0.388,
    5: 0.326,
    6: 0.300,
    7: 0.273,
    8: 0.247,
    9: 0.220,
    10: 0.194,
}
# Share of the transactional/home-services queries this tool audits that surface an AI Overview.
# AIOs grew from ~13% (mid-2024) toward ~40% of queries by late 2025; 0.40 is a conservative
# blend for commercial SERPs. Raising it makes every forecast more conservative (a larger upside
# haircut); it does not affect any score. This is the DEFAULT — env-tunable per deployment via
# GSC_OPPORTUNITY_AIO_PREVALENCE (the applied value is recorded in the stored estimate, so a
# tuned forecast stays self-describing).
_AIO_PREVALENCE = 0.40
# Even correct fixes rarely capture 100% of modeled upside; the headline is conservative.
# The capture ladder is part of the model's identity (versioned prose cites 50/70/100%), so it
# stays a constant rather than a per-deployment knob.
_SCENARIO_CAPTURE = {"conservative": 0.5, "expected": 0.7, "optimistic": 1.0}
# No scenario may exceed this multiple of the site's CURRENT monthly clicks — a
# step-function projection of many times current traffic is not defensible. DEFAULT only;
# env-tunable via GSC_OPPORTUNITY_CAP_MULTIPLE (recorded in the estimate as cap_multiple).
_OPPORTUNITY_CAP_MULTIPLE = 3.0
# Published home-services service-page contact (call/form) conversion benchmark RANGE — an industry
# figure, NOT measured on the audited site; only ever applied as a labeled range, never x job value.
_LEAD_RATE_LOW_PCT, _LEAD_RATE_HIGH_PCT = 5, 10

_CLUSTER_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "your",
        "you",
        "our",
        "best",
        "near",
        "me",
        "are",
        "can",
        "does",
        "how",
        "what",
        "when",
        "where",
        "why",
        "who",
        "cost",
        "costs",
        "price",
        "prices",
        "per",
        "much",
        "company",
        "companies",
        "service",
        "services",
        "contractor",
        "contractors",
        "home",
        "homes",
    }
)

# Function words that must never begin or end a topic label ("square foot to" -> "square
# foot"). Trimmed only at the edges, so they can still appear mid-phrase ("build a house").
_CLUSTER_EDGE_FILLERS = _CLUSTER_STOPWORDS | frozenset(
    {"to", "a", "an", "of", "in", "on", "at", "by", "or", "is", "it", "as", "vs"}
)


def _lookup_rank(curve: dict[int, float], position: int) -> float:
    """Read a per-rank curve value, clamping the position into the curve's 1..10 domain
    (every such curve defines all of ranks 1-10, so the clamp always resolves to a key)."""
    return curve[max(1, min(position, 10))]


def _ctr_at(position: int) -> float:
    return _lookup_rank(_CTR_CURVE, position)


def _aio_factor(position: int, prevalence: float = _AIO_PREVALENCE) -> float:
    """Position-aware AI-Overview upside multiplier: 1 - prevalence * CTR-reduction-at-rank.
    Higher ranks suffer more AIO suppression, so they keep a smaller share of the modeled
    upside. Always in (0, 1]; monotonically increasing in position (rank 1 keeps the least)."""
    return 1.0 - prevalence * _lookup_rank(_AIO_CTR_REDUCTION, position)


def _opportunity_for_target(rows: list[JsonDict], target_position: int) -> float:
    target_ctr = _ctr_at(target_position)
    total = 0.0
    for row in rows:
        impressions = float(row.get("impressions") or 0)
        current_ctr = float(row.get("ctr") or 0)
        # Never model a query BELOW its current real CTR; only the upside delta counts.
        total += max(0.0, impressions * (max(target_ctr, current_ctr) - current_ctr))
    return total


def _per_month(value: float, window_days: int) -> int:
    """Convert a collection-window total to a true monthly rate (half-up)."""
    months = max(window_days, 1) / _AVG_DAYS_PER_MONTH
    return int(value / months + 0.5)


def _opportunity_estimate(
    striking_rows: list[JsonDict],
    *,
    window_days: int,
    site_total_clicks: float,
    aio_prevalence: float = _AIO_PREVALENCE,
    cap_multiple: float = _OPPORTUNITY_CAP_MULTIPLE,
) -> JsonDict:
    """Deterministic 'clicks/leads left on the table' from the striking-distance set.

    Defensibility rules: model only the top-N striking queries; target positions 3-5,
    never 1; discount for AI Overviews; present conservative/expected/optimistic capture
    scenarios and lead with conservative; cap every scenario at a small multiple of the
    site's current monthly clicks; state true monthly rates with the window recorded.
    Every figure is stored as a fact so grounded prose can cite it. The AIO prevalence and
    the cap multiple are env-tunable (Settings) and both are recorded in the output, so a
    tuned deployment's stored estimates stay self-describing.
    """
    if not striking_rows:
        return {}
    modeled = sorted(
        striking_rows, key=lambda row: float(row.get("impressions") or 0), reverse=True
    )[:_OPPORTUNITY_MAX_QUERIES]
    total_impressions = sum(float(row.get("impressions") or 0) for row in striking_rows)
    current_clicks = sum(
        float(row.get("impressions") or 0) * float(row.get("ctr") or 0) for row in striking_rows
    )
    # Position-aware AIO haircut: each target rank keeps its own share of the upside (the
    # optimistic target 3 is discounted more than the conservative target 5).
    upside_low = _opportunity_for_target(modeled, _OPPORTUNITY_TARGET_LOW) * _aio_factor(
        _OPPORTUNITY_TARGET_LOW, aio_prevalence
    )
    upside_high = _opportunity_for_target(modeled, _OPPORTUNITY_TARGET_HIGH) * _aio_factor(
        _OPPORTUNITY_TARGET_HIGH, aio_prevalence
    )

    site_monthly_clicks = _per_month(site_total_clicks, window_days)
    # A zero-click site has no traffic baseline to cap against; the conservative capture
    # + AIO discount are the only brakes there (capping to max(0, ...) would pin every
    # scenario at a meaningless floor while the report claims "3x current clicks").
    # Half-up to an int so capped scenario figures stay whole clicks like everything else.
    cap = int(cap_multiple * site_monthly_clicks + 0.5) if site_monthly_clicks > 0 else None
    capped = False
    scenarios: JsonDict = {}
    for name, capture in _SCENARIO_CAPTURE.items():
        low = _per_month(upside_low * capture, window_days)
        high = _per_month(upside_high * capture, window_days)
        if cap is not None:
            if low > cap or high > cap:
                capped = True
            low = min(low, cap)
            high = min(high, cap)
        scenarios[name] = {
            "clicks_low": low,
            "clicks_high": high,
            "capture_pct": int(capture * 100),
        }

    headline = scenarios["conservative"]
    # The reader sees the conservative MONTHLY range; guard on that same number (the raw
    # window-total upside is ~6x larger at the default 91-day window and 50% capture, so
    # testing it lets a degenerate "0 to 0 visits per month" estimate through).
    if headline["clicks_high"] <= 0:
        return {}
    return {
        "is_estimate": True,
        "per_month": True,
        "window_days": window_days,
        "striking_query_count": len(striking_rows),
        "modeled_query_count": len(modeled),
        "striking_position_min": _STRIKING_POSITION_MIN,
        "striking_position_max": _STRIKING_POSITION_MAX,
        "total_striking_impressions": _per_month(total_impressions, window_days),
        "current_clicks": _per_month(current_clicks, window_days),
        "site_monthly_clicks": site_monthly_clicks,
        "scenarios": scenarios,
        # Headline = the conservative scenario, by design.
        "opportunity_clicks_low": headline["clicks_low"],
        "opportunity_clicks_high": headline["clicks_high"],
        "estimated_leads_low": int(headline["clicks_low"] * _LEAD_RATE_LOW_PCT / 100 + 0.5),
        "estimated_leads_high": int(headline["clicks_high"] * _LEAD_RATE_HIGH_PCT / 100 + 0.5),
        "lead_rate_low_pct": _LEAD_RATE_LOW_PCT,
        "lead_rate_high_pct": _LEAD_RATE_HIGH_PCT,
        "target_position_low": _OPPORTUNITY_TARGET_LOW,
        "target_position_high": _OPPORTUNITY_TARGET_HIGH,
        # Position-aware discount RANGE, derived from the SAME _aio_factor applied to the click
        # figures (single source of truth, so the displayed and applied haircuts never drift and
        # the target-position clamp is inherited): smaller at the conservative target (position 5),
        # larger at the optimistic one (position 3), where AI Overviews suppress clicks most.
        "aio_discount_min_pct": int(
            (1 - _aio_factor(_OPPORTUNITY_TARGET_LOW, aio_prevalence)) * 100 + 0.5
        ),
        "aio_discount_max_pct": int(
            (1 - _aio_factor(_OPPORTUNITY_TARGET_HIGH, aio_prevalence)) * 100 + 0.5
        ),
        "aio_prevalence_pct": int(aio_prevalence * 100 + 0.5),
        "aio_model_version": _AIO_MODEL_VERSION,
        "capture_capped": capped,
        "cap_applied": cap is not None,
        # Stored for prose/display: keep an integral multiple as an int so the client copy
        # reads "3x", not "3.0x".
        "cap_multiple": int(cap_multiple) if float(cap_multiple).is_integer() else cap_multiple,
        "ctr_curve_source": _CTR_CURVE_SOURCE,
        "ctr_curve_version": _CTR_CURVE_VERSION,
    }


# The shared brand/identity vocabulary now lives in technical_crawl_common (one home for the
# GSC branded split, the Places identity gate, and social discovery's brand tokens).
def _brand_token(site_url: str) -> str:
    return re.sub(r"[^a-z0-9]", "", registrable_brand_label(site_url))


def _branded_split(rows: list[JsonDict], site_url: str) -> JsonDict:
    """P3: branded vs non-branded demand split. A query is branded if the site's registrable name
    appears in its alphanumerics (so 'builder lead converter' matches builderleadconverter.com)."""
    token = _brand_token(site_url)
    if len(token) < 3:
        return {}
    branded_impr = branded_clicks = total_impr = total_clicks = 0.0
    branded_count = 0
    for row in rows:
        impressions = float(row.get("impressions") or 0)
        clicks = float(row.get("clicks") or 0)
        total_impr += impressions
        total_clicks += clicks
        if token in re.sub(r"[^a-z0-9]", "", str(row.get("query") or "").lower()):
            branded_impr += impressions
            branded_clicks += clicks
            branded_count += 1
    if total_impr <= 0:
        return {}
    return {
        "brand_token": token,
        "branded_query_count": branded_count,
        "branded_impressions": int(branded_impr + 0.5),
        "branded_clicks": int(branded_clicks + 0.5),
        "nonbranded_impressions": int(total_impr - branded_impr + 0.5),
        "nonbranded_clicks": int(total_clicks - branded_clicks + 0.5),
        "branded_impression_share_pct": int((branded_impr / total_impr) * 100 + 0.5),
    }


def _query_terms(query: str) -> list[str]:
    # \w is unicode-aware: "plomería" stays one term instead of fragmenting into
    # garbage pieces that would join back into unreadable phrase labels.
    return [term for term in re.split(r"[\W_]+", query.lower()) if term]


def _is_content_word(word: str) -> bool:
    """A word that carries topic meaning: at least 4 letters and not a cluster stopword.
    Single source of truth for what counts as a content word across tokenizing, phrase
    labelling, and cluster de-duplication (so the rule lives in one place, not four)."""
    return len(word) >= 4 and word not in _CLUSTER_STOPWORDS


def _query_ngrams(query: str, brand_token: str) -> list[str]:
    """Candidate topic labels for one query: readable phrases first (tri/bi-grams over the
    raw word sequence, so labels read like "cost per square foot" instead of disjoint
    tokens), with single content tokens as fallback granularity."""
    words = _query_terms(query)
    grams: list[str] = []
    for size in (3, 2):
        for start in range(len(words) - size + 1):
            piece = words[start : start + size]
            if brand_token and brand_token in piece:
                continue
            # Trim stopword/function-word edges so a label never reads "repair near me",
            # "square foot to", or "per square" — the rendered phrase keeps its subject
            # nouns. Only explicit fillers are stripped, so a meaningful short token like
            # "df" (Mexico City) or "ai" survives.
            while piece and piece[0] in _CLUSTER_EDGE_FILLERS:
                piece = piece[1:]
            while piece and piece[-1] in _CLUSTER_EDGE_FILLERS:
                piece = piece[:-1]
            if len(piece) < 2:
                continue
            # A phrase must carry at least one content word to be a topic label.
            if not any(_is_content_word(word) for word in piece):
                continue
            grams.append(" ".join(piece))
    grams.extend(_query_tokens(query, brand_token))
    return grams


def _query_tokens(query: str, brand_token: str) -> list[str]:
    return [
        token for token in _query_terms(query) if _is_content_word(token) and token != brand_token
    ]


def _content_chars(phrase: str) -> int:
    """Total length of a phrase's content words — a proxy for how much real meaning it
    carries, used to prefer "plomería méxico" over "méxico df"."""
    return sum(len(word) for word in phrase.split() if _is_content_word(word))


def _topic_clusters(
    rows: list[JsonDict], brand_token: str, *, max_clusters: int = 6
) -> list[JsonDict]:
    """P4: deterministic topic-cluster visibility. Groups queries by the heaviest CONTENT TOKEN
    they share (broad coverage — the way a marketer thinks in themes), but LABELS each group
    with its cleanest phrase so the report reads "square foot", not the bare token "square"
    (nor the two fragments "square" + "foot" the live run once showed). Co-occurring fragments
    are folded into one cluster. No LLM, no external taxonomy.

    Seeding on phrases directly (an earlier attempt) read well but under-counted: a broad query
    like "square footage estimate" contains no exact phrase seed and vanished, deflating every
    theme. Token grouping restores coverage; the phrase is used only for display.
    """
    token_weights: dict[str, float] = {}
    phrase_weights: dict[str, float] = {}
    for row in rows:
        impressions = float(row.get("impressions") or 0)
        query = str(row.get("query") or "")
        for token in set(_query_tokens(query, brand_token)):
            token_weights[token] = token_weights.get(token, 0.0) + impressions
        for gram in set(_query_ngrams(query, brand_token)):
            if len(gram.split()) >= 2:
                phrase_weights[gram] = phrase_weights.get(gram, 0.0) + impressions
    if not token_weights:
        return []

    def _label_for(token: str) -> str:
        # Cleanest phrase that includes this token as a word: prefer heavier, then SHORTER
        # (so "square foot" wins over "foot to build"), then more real content, then alpha.
        # Falls back to the bare token when no multi-word query contains it.
        best_key: tuple[float, int, int, str] | None = None
        chosen = token
        for phrase, weight in phrase_weights.items():
            if token not in phrase.split():
                continue
            key = (-weight, len(phrase.split()), -_content_chars(phrase), phrase)
            if best_key is None or key < best_key:
                best_key = key
                chosen = phrase
        return chosen

    seeds: list[tuple[str, str, set[str]]] = []  # (grouping token, display label, owned words)
    claimed: set[str] = set()
    for token, _weight in sorted(token_weights.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(seeds) >= max_clusters:
            break
        if token in claimed:
            continue
        label = _label_for(token)
        # A seed OWNS its grouping token plus every content word in its chosen label. Folding
        # those words into "claimed" stops a co-occurring fragment (the "foot" of "square
        # foot") from opening a second, duplicate cluster; OWNING them also means a query that
        # carries only that fragment still lands in THIS cluster (see bucketing below) rather
        # than being dropped — the 100% coverage the token-grouping rewrite is meant to give.
        owned = {token} | {word for word in label.split() if _is_content_word(word)}
        seeds.append((token, label, owned))
        claimed |= owned

    buckets: dict[str, dict[str, float]] = {
        token: {"impressions": 0.0, "position_weight": 0.0, "count": 0.0} for token, _, _ in seeds
    }
    labels = {token: label for token, label, _ in seeds}
    for row in rows:
        impressions = float(row.get("impressions") or 0)
        position = float(row.get("position") or 0)
        terms = set(_query_terms(str(row.get("query") or "")))
        # Prefer the heaviest seed whose GROUPING TOKEN the query actually contains — that is the
        # theme the query is genuinely about. Only when no grouping token matches (the query
        # shares just a folded label fragment) fall back to owned-word matching, which rescues a
        # fragment-only query into its fragment's cluster instead of dropping it, without
        # stealing a query that belongs to a lighter seed's own grouping token.
        token = next(
            (seed_token for seed_token, _label, _owned in seeds if seed_token in terms), None
        )
        if token is None:
            token = next((seed_token for seed_token, _label, owned in seeds if owned & terms), None)
        if token is None:
            continue
        bucket = buckets[token]
        bucket["impressions"] += impressions
        bucket["position_weight"] += position * impressions
        bucket["count"] += 1
    clusters = [
        {
            "cluster": labels[token],
            "query_count": int(bucket["count"]),
            "impressions": int(bucket["impressions"] + 0.5),
            "avg_position": round(bucket["position_weight"] / bucket["impressions"], 1)
            if bucket["impressions"] > 0
            else 0.0,
        }
        for token, bucket in buckets.items()
        if bucket["count"] > 0 and bucket["impressions"] > 0
    ]
    clusters.sort(key=lambda item: (-item["impressions"], item["cluster"]))
    return clusters


def _declining_pages(current_rows: list[JsonDict], previous_rows: list[JsonDict]) -> list[JsonDict]:
    previous_by_page = {str(row.get("page")): row for row in previous_rows if row.get("page")}
    declining = []
    for current in current_rows:
        page = str(current.get("page") or "")
        previous = previous_by_page.get(page)
        if not previous:
            continue
        click_delta = float(current.get("clicks") or 0) - float(previous.get("clicks") or 0)
        if click_delta <= -5:
            declining.append(
                {
                    "page": page,
                    "current_clicks": current.get("clicks"),
                    "previous_clicks": previous.get("clicks"),
                    "click_delta": round(click_delta, 2),
                    "current_impressions": current.get("impressions"),
                    "previous_impressions": previous.get("impressions"),
                }
            )
    return sorted(declining, key=lambda row: float(row["click_delta"]))


def _normalize_inspection(url: str, payload: JsonDict) -> JsonDict:
    result = payload.get("inspectionResult") or {}
    index_status = result.get("indexStatusResult") or {}
    verdict = str(index_status.get("verdict") or "").upper()
    coverage_state = str(index_status.get("coverageState") or "")
    on_google = True if verdict == "PASS" else False if verdict == "FAIL" else None
    return {
        "url": url,
        "status": "complete",
        "on_google": on_google,
        "verdict": verdict or None,
        "coverage_state": coverage_state or None,
        "robots_txt_state": index_status.get("robotsTxtState"),
        "indexing_state": index_status.get("indexingState"),
        "google_canonical": index_status.get("googleCanonical"),
        "user_canonical": index_status.get("userCanonical"),
        "last_crawl_time": index_status.get("lastCrawlTime"),
    }


def _priority_urls(url: str, page_urls: list[str], max_urls: int) -> list[str]:
    if max_urls <= 0:
        return []
    ordered = [url, *page_urls]
    seen: set[str] = set()
    selected = []
    for value in ordered:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            selected.append(cleaned)
        if len(selected) >= max_urls:
            break
    return selected


def _connection_properties(connection: GoogleSearchConsoleConnection) -> list[JsonDict]:
    properties = connection.properties if isinstance(connection.properties, dict) else {}
    entries = properties.get("siteEntry")
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def _expires_at(token_payload: JsonDict) -> datetime | None:
    expires_in = token_payload.get("expires_in")
    if not isinstance(expires_in, int | float):
        return None
    return datetime.now(UTC) + timedelta(seconds=int(expires_in))


def _normalized_url_for_match(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _external_google_payload(*, gsc: JsonDict, url_inspection: JsonDict) -> JsonDict:
    return {"gsc": gsc, "url_inspection": url_inspection}


def _skipped(reason: str, started_at: str) -> JsonDict:
    return {
        "status": "skipped",
        "reason": reason,
        "summary": {},
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _failed(error: str, started_at: str) -> JsonDict:
    return {
        "status": "failed",
        "error": error[:500],
        "summary": {},
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
