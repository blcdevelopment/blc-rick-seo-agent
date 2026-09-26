"""Adapter interface + registry + collector dispatch (P2-19 / SMWA-71)."""

from apps.shared.config import Settings
from apps.worker.stages.social import collector as collector_mod
from apps.worker.stages.social import providers as providers_mod
from apps.worker.stages.social.providers import (
    FacebookProvider,
    InstagramProvider,
    SocialProvider,
    YouTubeProvider,
    get_provider,
    supported_platforms,
)


def _settings(**overrides) -> Settings:
    base = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


def test_registry_covers_supported_platforms() -> None:
    assert set(supported_platforms()) == {"instagram", "facebook", "youtube"}
    for platform in supported_platforms():
        provider = get_provider(platform)
        assert provider is not None
        assert provider.platform == platform
        # Each registry entry honours the runtime-checkable adapter contract.
        assert isinstance(provider, SocialProvider)


def test_get_provider_unknown_platform_is_none() -> None:
    assert get_provider("tiktok") is None


def test_credential_available_reflects_settings() -> None:
    none = _settings()
    assert InstagramProvider().credential_available(none) is False
    assert FacebookProvider().credential_available(none) is False
    assert YouTubeProvider().credential_available(none) is False

    apify = _settings(apify_api_token="tok")
    assert InstagramProvider().credential_available(apify) is True
    assert FacebookProvider().credential_available(apify) is True
    assert YouTubeProvider().credential_available(apify) is False  # needs its own key

    youtube = _settings(youtube_api_key="key")
    assert YouTubeProvider().credential_available(youtube) is True
    assert InstagramProvider().credential_available(youtube) is False


def test_collect_skips_with_no_handles() -> None:
    facts = collector_mod.collect_social_facts(_settings(apify_api_token="t"), {})
    assert facts["status"] == "skipped"
    assert facts["reason"] == "no_social_handles"


def test_collect_skips_when_apify_token_missing() -> None:
    facts = collector_mod.collect_social_facts(_settings(), {"instagram": "acme"})
    assert facts["status"] == "skipped"
    assert facts["reason"] == "missing_apify_api_token"


def test_collect_skips_youtube_only_when_key_missing() -> None:
    facts = collector_mod.collect_social_facts(_settings(), {"youtube": "acme"})
    assert facts["status"] == "skipped"
    assert facts["reason"] == "missing_youtube_api_key"


def test_collect_dispatches_through_registry(monkeypatch) -> None:
    # No hardcoded platform branch: the collector fetches whatever provider the registry holds.
    monkeypatch.setattr(
        providers_mod,
        "fetch_instagram_profile",
        lambda handle, settings: {"followersCount": 100, "biography": "Call us"},
    )
    facts = collector_mod.collect_social_facts(
        _settings(apify_api_token="t"), {"instagram": "acme"}
    )
    assert facts["status"] == "complete"
    assert facts["platforms"][0]["platform"] == "instagram"
    assert facts["platforms"][0]["followers"] == 100


def test_facebook_provider_merges_posts_actor(monkeypatch) -> None:
    monkeypatch.setattr(
        providers_mod, "fetch_facebook_page", lambda handle, settings: {"pageName": "Acme"}
    )
    monkeypatch.setattr(
        providers_mod,
        "fetch_facebook_posts",
        lambda handle, settings: [{"time": "2026-06-01T00:00:00Z", "likes": 5}],
    )
    raw = FacebookProvider().fetch("acme", _settings(apify_api_token="t"))
    assert raw["pageName"] == "Acme"
    assert raw["posts"] == [{"time": "2026-06-01T00:00:00Z", "likes": 5}]


def test_facebook_provider_returns_none_when_page_missing(monkeypatch) -> None:
    monkeypatch.setattr(providers_mod, "fetch_facebook_page", lambda handle, settings: None)
    assert FacebookProvider().fetch("acme", _settings(apify_api_token="t")) is None


def test_actor_url_passes_links_through_and_nests_bare_handles() -> None:
    # The actor target URL shares the ONE URL-shaped-handle detector: a scheme-less link the
    # rest of the pipeline accepts must not be nested into a doubled-domain fetch URL.
    from apps.worker.stages.social.apify_provider import _actor_url

    assert (
        _actor_url("www.instagram.com/acme", "https://www.instagram.com")
        == "https://www.instagram.com/acme"
    )
    assert (
        _actor_url("https://www.facebook.com/acme/", "https://www.facebook.com")
        == "https://www.facebook.com/acme/"
    )
    assert _actor_url("@acme", "https://www.instagram.com") == "https://www.instagram.com/acme/"


def test_apify_token_travels_in_the_auth_header_not_the_url(monkeypatch) -> None:
    # httpx logs full request URLs at INFO; a ?token= query param would leak into worker logs.
    from apps.worker.stages.social import apify_provider

    calls: list[dict] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> list[dict]:
            return [{"username": "acme"}]

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr(apify_provider.httpx, "post", fake_post)

    raw = apify_provider.fetch_instagram_profile("acme", _settings(apify_api_token="secret-tok"))

    assert raw == {"username": "acme"}
    (call,) = calls
    assert call["headers"] == {"Authorization": "Bearer secret-tok"}
    assert "params" not in call
    assert "secret-tok" not in call["url"]
