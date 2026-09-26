from apps.shared.config import Settings
from apps.worker.stages.social.collector import collect_social_facts
from apps.worker.stages.social.youtube_provider import _channel_lookups


def test_lookups_bare_handle() -> None:
    assert _channel_lookups("@acme") == [{"forHandle": "@acme"}, {"forUsername": "acme"}]
    assert _channel_lookups("acme") == [{"forHandle": "@acme"}, {"forUsername": "acme"}]


def test_lookups_channel_id() -> None:
    cid = "UC" + "a" * 22  # 24 chars
    assert _channel_lookups(cid) == [{"id": cid}]
    assert _channel_lookups(f"https://youtube.com/channel/{cid}") == [{"id": cid}]


def test_lookups_handle_url_with_subpath() -> None:
    # /@acme/videos must resolve the handle, not the trailing 'videos' segment.
    assert _channel_lookups("https://youtube.com/@acme/videos") == [
        {"forHandle": "@acme"},
        {"forUsername": "acme"},
    ]
    assert _channel_lookups("https://www.youtube.com/@acme") == [
        {"forHandle": "@acme"},
        {"forUsername": "acme"},
    ]


def test_lookups_legacy_custom_url() -> None:
    assert _channel_lookups("https://youtube.com/c/AcmeBuilders") == [
        {"forUsername": "AcmeBuilders"},
        {"forHandle": "@AcmeBuilders"},
    ]


def _settings(**overrides) -> Settings:
    base = {"_env_file": None, "apify_api_token": None, "youtube_api_key": None}
    base.update(overrides)
    return Settings(**base)


def test_collector_youtube_only_missing_key_skips() -> None:
    out = collect_social_facts(_settings(), {"youtube": "@acme"})
    assert out["status"] == "skipped"
    assert out["reason"] == "missing_youtube_api_key"


def test_collector_instagram_only_missing_token_skips() -> None:
    out = collect_social_facts(_settings(), {"instagram": "acme"})
    assert out["status"] == "skipped"
    assert out["reason"] == "missing_apify_api_token"


def test_collector_no_handles_skips() -> None:
    out = collect_social_facts(_settings(), {})
    assert out["status"] == "skipped"
    assert out["reason"] == "no_social_handles"


def test_channel_lookups_handle_protocol_relative_links() -> None:
    # Auto-discovery/pasted links can be protocol-relative. A bare f"https://{value}" would
    # build "https:////www.youtube.com/..." and urlparse would leave the HOST in the path, so
    # every lookup resolved against "www.youtube.com" instead of the channel.
    from apps.worker.stages.social.youtube_provider import _channel_lookups

    assert _channel_lookups("//www.youtube.com/c/AcmeTV") == [
        {"forUsername": "AcmeTV"},
        {"forHandle": "@AcmeTV"},
    ]
    assert _channel_lookups("//www.youtube.com/@AcmeTV") == [
        {"forHandle": "@AcmeTV"},
        {"forUsername": "AcmeTV"},
    ]
    assert _channel_lookups("www.youtube.com/channel/UC" + "x" * 22) == [{"id": "UC" + "x" * 22}]


def test_youtube_key_travels_in_the_header_not_the_url(monkeypatch) -> None:
    # httpx logs full request URLs at INFO; a ?key= query param would leak into worker logs.
    import httpx

    from apps.worker.stages.social import youtube_provider

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{"id": "UC" + "a" * 22, "contentDetails": {}}]})

    real_client = httpx.Client
    monkeypatch.setattr(
        youtube_provider.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    raw = youtube_provider.fetch_youtube_channel("@acme", _settings(youtube_api_key="yt-secret"))

    assert raw is not None
    assert raw["videos"] == []
    assert seen
    for request in seen:
        assert request.headers["x-goog-api-key"] == "yt-secret"
        assert "key" not in request.url.params
        assert "yt-secret" not in str(request.url)
