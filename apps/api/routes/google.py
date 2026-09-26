from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from apps.api.auth import require_user
from apps.api.deps import get_db_session
from apps.shared.config import Settings, get_settings
from apps.worker.stages.google_search_console import (
    build_google_oauth_url,
    ensure_google_access_token,
    exchange_google_oauth_code,
    google_oauth_configured,
    google_userinfo,
    latest_google_connection,
    list_search_console_sites,
    upsert_google_connection,
)

DbSession = Annotated[Session, Depends(get_db_session)]
router = APIRouter(prefix="/google/search-console", tags=["google-search-console"])

# CSRF protection for the OAuth flow: `state` is an HMAC-signed, time-limited
# token issued only to an authenticated operator, and the (necessarily
# unauthenticated) callback refuses to store a Google connection without a valid
# one. Without this, anyone who could reach the callback could plant their own
# Google account as the workspace's Search Console connection.
_STATE_TTL_SECONDS = 600
_EPHEMERAL_STATE_SECRET = secrets.token_bytes(32)

_logger = logging.getLogger(__name__)
_ephemeral_secret_warned = False


def _state_secret(settings: Settings) -> bytes:
    configured = getattr(settings, "google_oauth_state_secret", None)
    if configured and configured.get_secret_value():
        return hashlib.sha256(configured.get_secret_value().encode("utf-8")).digest()
    # No configured secret: fall back to a per-process random key. Fine for a single API
    # process, but OAuth `state` issued by one process can't be validated by another, so the
    # flow breaks across restarts / multiple replicas. Warn once so this isn't a silent footgun.
    global _ephemeral_secret_warned
    if not _ephemeral_secret_warned:
        _ephemeral_secret_warned = True
        _logger.warning(
            "GOOGLE_OAUTH_STATE_SECRET is not set; using an ephemeral per-process secret for the "
            "Search Console OAuth `state`. This breaks the OAuth flow across API restarts or "
            "multiple replicas. Set it to a fixed random value in production."
        )
    return _EPHEMERAL_STATE_SECRET


def _issue_oauth_state(settings: Settings) -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(16)}"
    signature = hmac.new(
        _state_secret(settings), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _oauth_state_valid(settings: Settings, state: str | None) -> bool:
    parts = (state or "").split(".")
    if len(parts) != 3:
        return False
    issued_at_raw, nonce, signature = parts
    expected = hmac.new(
        _state_secret(settings),
        f"{issued_at_raw}.{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        issued_at = int(issued_at_raw)
    except ValueError:
        return False
    return 0 <= time.time() - issued_at <= _STATE_TTL_SECONDS


class SearchConsoleProperty(BaseModel):
    model_config = ConfigDict(extra="allow")

    siteUrl: str
    permissionLevel: str | None = None


class SearchConsolePropertiesResponse(BaseModel):
    status: str
    account_email: str | None = None
    properties: list[SearchConsoleProperty] = Field(default_factory=list)
    reason: str | None = None


class SearchConsoleConnectUrlResponse(BaseModel):
    status: str
    connect_url: str | None = None
    reason: str | None = None


@router.get("/connect", dependencies=[Depends(require_user)])
def connect_search_console() -> RedirectResponse:
    settings = get_settings()
    if not google_oauth_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured.",
        )
    return RedirectResponse(
        build_google_oauth_url(settings, state=_issue_oauth_state(settings)),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.get(
    "/connect-url",
    response_model=SearchConsoleConnectUrlResponse,
    dependencies=[Depends(require_user)],
)
def get_search_console_connect_url() -> SearchConsoleConnectUrlResponse:
    """Return a Google OAuth URL after Clerk has authenticated the app user.

    Frontend navigation cannot attach a Clerk bearer token to a plain cross-origin
    link. This endpoint lets the signed-in UI request the OAuth destination with
    its Clerk token, then assign ``window.location`` to the returned Google URL.
    """
    settings = get_settings()
    if not google_oauth_configured(settings):
        return SearchConsoleConnectUrlResponse(
            status="skipped",
            reason="oauth_not_configured",
        )
    return SearchConsoleConnectUrlResponse(
        status="ready",
        connect_url=build_google_oauth_url(settings, state=_issue_oauth_state(settings)),
    )


@router.get("/callback", include_in_schema=False)
def search_console_callback(
    db: DbSession,
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    state: str | None = Query(default=None),
) -> RedirectResponse:
    settings = get_settings()
    if error:
        return _redirect_with_status(settings.google_oauth_success_redirect_url, "error", error)
    if not code:
        return _redirect_with_status(
            settings.google_oauth_success_redirect_url,
            "error",
            "missing_code",
        )
    if not google_oauth_configured(settings):
        return _redirect_with_status(
            settings.google_oauth_success_redirect_url,
            "error",
            "oauth_not_configured",
        )
    if not _oauth_state_valid(settings, state):
        return _redirect_with_status(
            settings.google_oauth_success_redirect_url,
            "error",
            "invalid_state_restart_connect",
        )

    try:
        token_payload = exchange_google_oauth_code(code, settings)
        access_token = str(token_payload["access_token"])
        userinfo = google_userinfo(access_token)
        account_email = _account_email(userinfo)
        properties = list_search_console_sites(access_token)
        upsert_google_connection(
            db,
            account_email=account_email,
            token_payload=token_payload,
            properties=properties,
        )
    except Exception:  # noqa: BLE001 - never leak token exchange details into redirects
        return _redirect_with_status(
            settings.google_oauth_success_redirect_url,
            "error",
            "google_connection_failed",
        )

    return _redirect_with_status(
        settings.google_oauth_success_redirect_url,
        "connected",
        f"{len(properties)}_properties",
    )


@router.get(
    "/properties",
    response_model=SearchConsolePropertiesResponse,
    dependencies=[Depends(require_user)],
)
def get_search_console_properties(db: DbSession) -> SearchConsolePropertiesResponse:
    settings = get_settings()
    if not google_oauth_configured(settings):
        return SearchConsolePropertiesResponse(status="skipped", reason="oauth_not_configured")

    connection = latest_google_connection(db)
    if connection is None:
        return SearchConsolePropertiesResponse(status="skipped", reason="no_google_connection")

    try:
        access_token = ensure_google_access_token(connection, settings, db)
        properties = list_search_console_sites(access_token)
        connection.properties = {"siteEntry": properties}
        db.commit()
        db.refresh(connection)
    except Exception as exc:  # noqa: BLE001 - sanitize tokens but preserve operator signal
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not refresh Search Console properties: {type(exc).__name__}",
        ) from exc

    return SearchConsolePropertiesResponse(
        status="complete",
        account_email=connection.account_email,
        properties=[SearchConsoleProperty.model_validate(item) for item in properties],
    )


def _account_email(userinfo: dict[str, Any]) -> str:
    email = str(userinfo.get("email") or "").strip()
    if email:
        return email
    subject = str(userinfo.get("sub") or "").strip()
    if subject:
        return f"google-sub-{subject}@search-console.local"
    raise ValueError("Google userinfo did not include an email or subject.")


def _redirect_with_status(base_url: str, status_value: str, detail: str) -> RedirectResponse:
    separator = "&" if "?" in base_url else "?"
    query = urlencode({"gsc": status_value, "detail": detail})
    return RedirectResponse(
        f"{base_url}{separator}{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
