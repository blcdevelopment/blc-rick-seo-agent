"""Clerk session-token verification for the API.

The operator UI is gated by Clerk; the browser then carries the Clerk session token
to the API either in the ``__session`` cookie (same-origin in production) or an
``Authorization: Bearer`` header. This module verifies that token against Clerk's
public keys (JWKS) so the audit endpoints are not open to the internet.

Auth is OPT-IN via ``CLERK_ISSUER``: when it is empty (local dev / tests / the QA
harness) verification is skipped and endpoints behave exactly as before. Set
``CLERK_ISSUER`` in production to enforce it.
"""

from __future__ import annotations

from functools import lru_cache

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

from apps.shared.config import get_settings

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


@lru_cache(maxsize=4)
def _jwk_client(jwks_url: str) -> PyJWKClient:
    # PyJWKClient caches the fetched signing keys internally, so reuse one per URL.
    return PyJWKClient(jwks_url)


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer ") :].strip() or None
    return request.cookies.get("__session")


def require_user(request: Request) -> str | None:
    """FastAPI dependency: return the Clerk user id (``sub``) or raise 401.

    Returns ``None`` and allows the request when Clerk is not configured.
    """
    settings = get_settings()
    issuer = settings.clerk_issuer.rstrip("/")
    if not issuer:
        return None  # auth disabled (no CLERK_ISSUER set)

    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Clerk session token.",
            headers=_UNAUTHORIZED,
        )
    try:
        signing_key = _jwk_client(f"{issuer}/.well-known/jwks.json").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            leeway=10,  # tolerate Clerk's ~60s token lifetime + minor clock skew
            options={"verify_aud": False},
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 401
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Clerk session token.",
            headers=_UNAUTHORIZED,
        ) from exc

    # Authorized-party (origin) check. When parties are configured the token MUST carry an
    # allowed `azp` — a token that simply omits the claim no longer slips past the check.
    parties = settings.clerk_authorized_parties
    if parties:
        azp = claims.get("azp")
        if not azp or azp not in parties:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token authorized party is not allowed.",
                headers=_UNAUTHORIZED,
            )

    sub = claims.get("sub")
    # Optional defense-in-depth: restrict the API to a known set of Clerk user ids so a stranger
    # who self-registers on the Clerk instance can't reach the endpoints. Empty => no restriction.
    allowed_subjects = settings.clerk_allowed_subjects
    if allowed_subjects and sub not in allowed_subjects:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not authorized to use the audit tool.",
            headers=_UNAUTHORIZED,
        )
    return sub
