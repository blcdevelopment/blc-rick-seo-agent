"""YouTube Data API v3 backend for the social audit.

Fetches a public channel's stats + recent uploads via the official Data API (a plain
API key — no OAuth, public data only). Network-only; returns the raw payload
``{"channel": <channels item>, "videos": [<videos items>]}`` or ``None`` so the collector
degrades gracefully (like a missing Apify token). The key is read from Settings and sent only in
the ``x-goog-api-key`` header (as psi_client does) — never in the URL, where httpx's INFO request
log would expose it. One channel audit costs only a few quota units (channels.list +
playlistItems + videos = ~3 of the ~10,000/day free quota).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings
from apps.worker.stages.social.extractor import profile_link_from_handle

_YT_API = "https://www.googleapis.com/youtube/v3"
_CHANNEL_PARTS = "snippet,statistics,contentDetails,brandingSettings"
_RESULTS_LIMIT = 12

JsonDict = dict[str, Any]


def _channel_lookups(handle: str) -> list[dict[str, str]]:
    """Ordered channels.list lookups to try for a handle/ID/URL (each is 1 quota unit).

    Note: the API has no direct lookup for legacy ``/c/CustomName`` URLs — we try
    forUsername then forHandle, which resolves only when the slug matches a real username
    or the channel's @handle. When it doesn't, the fetch returns None and the channel is
    recorded as ``failed`` (graceful skip, never aborts). Prefer an @handle or channel ID.
    """
    value = handle.strip()
    # Normalize a URL-shaped handle through the ONE shared detector first: it also recognizes
    # the protocol-relative form ("//www.youtube.com/c/AcmeTV"), which a bare `f"https://{value}"`
    # would turn into "https:////www.youtube.com/..." — urlparse then leaves the HOST in the path
    # and every lookup below resolves against "www.youtube.com" instead of the channel.
    value = profile_link_from_handle(value) or value
    if "youtube.com" in value.lower() or value.lower().startswith("http"):
        path = urlparse(value if "://" in value else f"https://{value}").path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "channel":
            return [{"id": parts[1]}]
        if len(parts) >= 2 and parts[0] in {"c", "user"}:
            return [{"forUsername": parts[1]}, {"forHandle": f"@{parts[1]}"}]
        # /@handle (optionally with a /videos, /about, /featured sub-path) or a bare
        # /CustomName: take the first handle/ID-looking segment, not the trailing sub-path.
        value = next(
            (p for p in parts if p.startswith("@") or (p.startswith("UC") and len(p) == 24)),
            parts[0] if parts else "",
        )
    value = value.lstrip("@").strip("/")
    if not value:
        return []
    if value.startswith("UC") and len(value) == 24:
        return [{"id": value}]
    # Modern channels use @handles; fall back to a legacy custom username.
    return [{"forHandle": f"@{value}"}, {"forUsername": value}]


def _get(client: httpx.Client, path: str, params: dict[str, Any], key: str) -> JsonDict | None:
    response = client.get(f"{_YT_API}/{path}", params=params, headers={"x-goog-api-key": key})
    if response.status_code >= 400:
        return None
    data = response.json()
    return data if isinstance(data, dict) else None


def _items(data: JsonDict | None) -> list[JsonDict]:
    items = data.get("items") if isinstance(data, dict) else None
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


def fetch_youtube_channel(handle: str, settings: Settings) -> JsonDict | None:
    if not handle:
        return None
    key = settings.youtube_api_key.get_secret_value() if settings.youtube_api_key else ""
    if not key:
        return None
    lookups = _channel_lookups(handle)
    if not lookups:
        return None
    try:
        with httpx.Client(timeout=settings.youtube_timeout_seconds) as client:
            channel: JsonDict | None = None
            for params in lookups:
                found = _items(_get(client, "channels", {"part": _CHANNEL_PARTS, **params}, key))
                if found:
                    channel = found[0]
                    break
            if channel is None:
                return None

            uploads = (
                (channel.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
            )
            videos: list[JsonDict] = []
            if uploads:
                playlist = _get(
                    client,
                    "playlistItems",
                    {"part": "contentDetails", "playlistId": uploads, "maxResults": _RESULTS_LIMIT},
                    key,
                )
                video_ids = [
                    (it.get("contentDetails") or {}).get("videoId") for it in _items(playlist)
                ]
                video_ids = [vid for vid in video_ids if vid]
                if video_ids:
                    videos = _items(
                        _get(
                            client,
                            "videos",
                            {"part": "snippet,statistics", "id": ",".join(video_ids)},
                            key,
                        )
                    )
            return {"channel": channel, "videos": videos}
    except SoftTimeLimitExceeded:
        # The worker is out of time: propagate so the task can mark the job failed honestly
        # instead of the hard limit killing it mid-pipeline (the sibling providers and the
        # crawler follow the same convention; swallowing it here would defeat the caller's
        # "only SoftTimeLimitExceeded propagates" contract — Celery raises it exactly once).
        raise
    except Exception:
        return None
