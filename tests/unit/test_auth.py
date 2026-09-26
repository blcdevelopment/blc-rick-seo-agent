"""Unit tests for the Clerk session-token gate (apps/api/auth.require_user).

JWT signature verification is bypassed here (we monkeypatch the JWKS client + jwt.decode);
the focus is the gate's own logic: opt-in via CLERK_ISSUER, the authorized-party (azp) check,
and the optional subject allowlist. Settings are built with _env_file=None so a developer's
local .env can't leak into the assertions.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import apps.api.auth as auth_module
from apps.shared.config import Settings

_ISSUER = "https://example.clerk.accounts.dev"


class _Req:
    def __init__(self, *, bearer: str | None = None, cookie: str | None = None) -> None:
        self.headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        self.cookies = {"__session": cookie} if cookie else {}


class _FakeJwk:
    def get_signing_key_from_jwt(self, token: str):  # noqa: ANN001 - test double
        class _Key:
            key = "fake-signing-key"

        return _Key()


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **kwargs) -> Settings:
    settings = Settings(_env_file=None, **kwargs)
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    return settings


def _patch_decode(monkeypatch: pytest.MonkeyPatch, claims: dict) -> None:
    monkeypatch.setattr(auth_module, "_jwk_client", lambda url: _FakeJwk())
    monkeypatch.setattr(auth_module.jwt, "decode", lambda *a, **k: claims)


def test_auth_disabled_returns_none_without_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, clerk_issuer="")
    assert auth_module.require_user(_Req()) is None


def test_missing_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, clerk_issuer=_ISSUER)
    with pytest.raises(HTTPException) as exc:
        auth_module.require_user(_Req())
    assert exc.value.status_code == 401


def test_valid_token_returns_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, clerk_issuer=_ISSUER)
    _patch_decode(monkeypatch, {"sub": "user_1"})
    assert auth_module.require_user(_Req(bearer="t")) == "user_1"


def test_azp_is_required_when_parties_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hardened: a token that simply omits `azp` must NOT slip past the party check.
    _patch_settings(
        monkeypatch, clerk_issuer=_ISSUER, clerk_authorized_parties="https://app.example.com"
    )
    _patch_decode(monkeypatch, {"sub": "user_1"})  # no azp claim
    with pytest.raises(HTTPException) as exc:
        auth_module.require_user(_Req(bearer="t"))
    assert exc.value.status_code == 401


def test_disallowed_azp_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch, clerk_issuer=_ISSUER, clerk_authorized_parties="https://app.example.com"
    )
    _patch_decode(monkeypatch, {"sub": "user_1", "azp": "https://evil.example.com"})
    with pytest.raises(HTTPException) as exc:
        auth_module.require_user(_Req(bearer="t"))
    assert exc.value.status_code == 401


def test_allowed_azp_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch, clerk_issuer=_ISSUER, clerk_authorized_parties="https://app.example.com"
    )
    _patch_decode(monkeypatch, {"sub": "user_1", "azp": "https://app.example.com"})
    assert auth_module.require_user(_Req(bearer="t")) == "user_1"


def test_subject_allowlist_blocks_unknown_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, clerk_issuer=_ISSUER, clerk_allowed_subjects="user_allowed")
    _patch_decode(monkeypatch, {"sub": "user_stranger"})
    with pytest.raises(HTTPException) as exc:
        auth_module.require_user(_Req(bearer="t"))
    assert exc.value.status_code == 403


def test_subject_allowlist_permits_known_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch, clerk_issuer=_ISSUER, clerk_allowed_subjects="user_allowed, user_two"
    )
    _patch_decode(monkeypatch, {"sub": "user_two"})
    assert auth_module.require_user(_Req(bearer="t")) == "user_two"
