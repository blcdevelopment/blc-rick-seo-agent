from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

JsonDict = dict[str, Any]
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _remote_logo_url_allowed(url: str) -> bool:
    """Vet a white-label ``logo_url`` before letting WeasyPrint fetch it at render time.

    The PDF renderer fetches this URL **server-side** from the worker, so an operator-supplied
    override pointing at a private/loopback/link-local/reserved host (e.g. the cloud-metadata
    endpoint ``169.254.169.254``) would be a server-side request forgery (SSRF) vector. This
    mirrors the crawler's host vetting (``apps/worker/stages/crawler.py``) but is kept
    self-contained so the render path doesn't import the Playwright-heavy crawler module.

    An IP literal is checked directly; a hostname is DNS-resolved and rejected if ANY resolved
    address is internal (so a public name pointing at an internal IP is caught too). A host that
    fails to resolve is allowed — WeasyPrint will simply fail to fetch it, and the override is
    then dropped like any other malformed value.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        return not _ip_is_blocked(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass  # not an IP literal -> resolve the hostname below
    try:
        resolved = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return True  # unresolvable: the render-time fetch will fail; not an SSRF risk
    for info in resolved:
        raw_address = str(info[4][0]).split("%")[0]
        try:
            if _ip_is_blocked(ipaddress.ip_address(raw_address)):
                return False
        except ValueError:
            continue
    return True


class BrandConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Builder Lead Converter"
    short_name: str = "BLC"
    # The audit product's name on the report cover/title ("Gooch · Comprehensive Website
    # Audit"). Config-driven (not a template literal) so a white-label run can replace it
    # via brand_overrides instead of shipping a client a PDF branded with our tool name.
    product_name: str = "Gooch"
    logo_path: Path | None = None
    primary_color: str = "#1a3a5c"
    accent_color: str = "#f5a623"
    font_heading: str = "Inter"
    font_body: str = "Inter"
    placeholder_fallback: str = "BLC"

    @field_validator(
        "name", "short_name", "product_name", "font_heading", "font_body", "placeholder_fallback"
    )
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("primary_color", "accent_color")
    @classmethod
    def valid_hex_color(cls, value: str) -> str:
        cleaned = value.strip()
        if not HEX_COLOR_RE.match(cleaned):
            raise ValueError("must be a 6-digit hex color, for example #1a3a5c")
        return cleaned

    def template_context(self, *, config_path: Path | None = None) -> JsonDict:
        resolved_logo = self._resolved_logo_path(config_path)
        return {
            "name": self.name,
            "short_name": self.short_name,
            "product_name": self.product_name,
            "primary_color": self.primary_color,
            "accent_color": self.accent_color,
            "font_heading": self.font_heading,
            "font_body": self.font_body,
            "placeholder_fallback": self.placeholder_fallback,
            "logo_uri": resolved_logo.as_uri() if resolved_logo else None,
            "logo_path": str(resolved_logo) if resolved_logo else None,
            "logo_exists": resolved_logo is not None,
        }

    def _resolved_logo_path(self, config_path: Path | None) -> Path | None:
        if self.logo_path is None:
            return None

        candidate = self.logo_path
        if not candidate.is_absolute():
            roots = []
            if config_path is not None:
                roots.append(config_path.parent)
            roots.append(Path.cwd())
            for root in roots:
                resolved = (root / candidate).resolve()
                if resolved.exists():
                    return resolved
            return None

        resolved = candidate.resolve()
        return resolved if resolved.exists() else None


def load_brand_config(path: Path) -> BrandConfig:
    if not path.exists():
        return BrandConfig()

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Brand config {path} must contain a YAML object.")
    return BrandConfig.model_validate(payload)


def apply_brand_overrides(context: JsonDict, overrides: JsonDict | None) -> JsonDict:
    """Merge per-audit white-label overrides over a brand template context. Only known,
    validated fields are applied; anything missing or malformed is ignored so a bad
    override can never break rendering. ``logo_url`` (http/https) replaces the logo with a
    remote image WeasyPrint fetches at render time — it is SSRF-vetted via
    ``_remote_logo_url_allowed`` so it can't point the server-side fetch at an internal host."""
    if not overrides or not isinstance(overrides, dict):
        return context

    merged = dict(context)
    for key in ("name", "short_name", "product_name"):
        value = overrides.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = " ".join(value.split())
    for key in ("primary_color", "accent_color"):
        value = overrides.get(key)
        if isinstance(value, str) and HEX_COLOR_RE.match(value.strip()):
            merged[key] = value.strip()
    logo_url = overrides.get("logo_url")
    if (
        isinstance(logo_url, str)
        and logo_url.strip().lower().startswith(("http://", "https://"))
        and _remote_logo_url_allowed(logo_url.strip())
    ):
        merged["logo_uri"] = logo_url.strip()
        merged["logo_path"] = None
        merged["logo_exists"] = True
    return merged
