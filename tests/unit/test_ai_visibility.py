"""AI Visibility enrichment (Semrush) — provider registry + credentials, graceful-skip collector,
defensive normalizer, and the pure report builder.

AI visibility is a graceful-skip, presentation-only enrichment: OFF by default, it must never raise
into the caller, never fabricate a section, and never touch a score. These tests exercise the whole
layer WITHOUT the network / OpenAI / Playwright — the provider fetch is monkeypatched, exactly the
seam the real Celery task drives.
"""

from apps.shared.config import Settings
from apps.worker.stages.ai_visibility import collector as collector_mod
from apps.worker.stages.ai_visibility.collector import (
    collect_ai_visibility_facts,
    normalize_ai_visibility_facts,
)
from apps.worker.stages.ai_visibility.providers import (
    AiVisibilityProvider,
    get_provider,
    supported_providers,
)
from apps.worker.stages.ai_visibility.report import build_ai_visibility_report_data


def _settings(**overrides) -> Settings:
    base = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


_FULL_RAW = {
    "visibility_score": 19,
    "visibility_band": "Low",
    "mentions": 28,
    "citations": 380,
    "cited_pages": 42,
    "share_of_voice_pct": 12.5,
    "per_platform": [
        {"platform": "AI Overview", "mentions": 22, "share_pct": 78.6},
        {"platform": "AI Mode", "mentions": 4, "share_pct": 14.3},
        {"platform": "ChatGPT", "mentions": 0, "share_pct": 0.0},
    ],
    "topics": [
        {
            "topic": "House Construction Cost Estimation",
            "visibility": 11,
            "your_mentions": 10,
            "ai_volume": "116.9K",
        },
    ],
    "competitors": [{"label": "nahb.org", "visibility_score": 40, "mentions": 50}],
    "by_country": [{"country": "US", "mentions": 28, "share_pct": 100.0}],
}


class _FakeProvider:
    """A registry-shaped provider whose credentials are present and whose fetch is scripted."""

    name = "semrush"

    def __init__(self, result):
        self._result = result

    def credential_available(self, settings) -> bool:
        return True

    def fetch(self, *, domain, settings):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _patch_provider(monkeypatch, result) -> None:
    monkeypatch.setattr(collector_mod, "get_provider", lambda name: _FakeProvider(result))


# --- registry + credentials -----------------------------------------------------------------


def test_registry_exposes_semrush() -> None:
    assert set(supported_providers()) == {"semrush"}
    provider = get_provider("semrush")
    assert provider is not None
    assert provider.name == "semrush"
    assert isinstance(provider, AiVisibilityProvider)
    assert get_provider("bogus") is None


def test_semrush_credentials_require_openai_and_a_login_or_session(tmp_path) -> None:
    provider = get_provider("semrush")
    # Point the session path at a guaranteed-missing file so the check is independent of whether a
    # real ./storage/semrush_session.json happens to exist on the dev machine.
    no_session = str(tmp_path / "missing.json")
    # Nothing configured => unavailable.
    assert provider.credential_available(_settings(semrush_session_state_path=no_session)) is False
    # OpenAI only, but no session and no email => unavailable.
    assert (
        provider.credential_available(
            _settings(openai_api_key="k", semrush_session_state_path=no_session)
        )
        is False
    )
    # OpenAI + email/password => available.
    assert (
        provider.credential_available(
            _settings(openai_api_key="k", semrush_email="e@x.com", semrush_password="p")
        )
        is True
    )
    # OpenAI + an existing saved session file => available (no password needed).
    session_file = tmp_path / "semrush_session.json"
    session_file.write_text("{}", encoding="utf-8")
    assert (
        provider.credential_available(
            _settings(openai_api_key="k", semrush_session_state_path=str(session_file))
        )
        is True
    )
    # A configured session PATH that does not exist yet is not a usable credential on its own.
    assert (
        provider.credential_available(
            _settings(openai_api_key="k", semrush_session_state_path=str(tmp_path / "missing.json"))
        )
        is False
    )


# --- collector graceful skip ----------------------------------------------------------------


def test_collect_skips_when_disabled_by_default() -> None:
    facts = collect_ai_visibility_facts(_settings(), domain="acme.test")
    assert facts["status"] == "skipped"
    assert facts["reason"] == "ai_visibility_disabled"


def test_collect_skips_for_unknown_provider() -> None:
    facts = collect_ai_visibility_facts(
        _settings(ai_visibility_enabled=True, ai_visibility_provider="bogus"),
        domain="acme.test",
    )
    assert facts["status"] == "skipped"
    assert facts["reason"] == "no_ai_visibility_provider"


def test_collect_skips_without_credentials() -> None:
    # Enabled + semrush selected, but no OpenAI key / login configured.
    facts = collect_ai_visibility_facts(_settings(ai_visibility_enabled=True), domain="acme.test")
    assert facts["status"] == "skipped"
    assert facts["reason"] == "missing_credentials"


def test_collect_fetch_returning_none_marks_failed(monkeypatch) -> None:
    # The bot ran but got nothing => a visible "could not retrieve" note (status failed), not a
    # silent skip.
    _patch_provider(monkeypatch, None)
    facts = collect_ai_visibility_facts(_settings(ai_visibility_enabled=True), domain="acme.test")
    assert facts["status"] == "failed"
    assert facts["reason"] == "unavailable"
    assert facts["domain"] == "acme.test"


def test_collect_fetch_raising_is_backstopped_as_failed(monkeypatch) -> None:
    # A bug in the bot must never propagate into the calling task; it renders a note.
    _patch_provider(monkeypatch, RuntimeError("boom"))
    facts = collect_ai_visibility_facts(_settings(ai_visibility_enabled=True), domain="acme.test")
    assert facts["status"] == "failed"
    assert facts["reason"] == "error"


def test_collect_captcha_block_marks_failed_with_reason(monkeypatch) -> None:
    # A CAPTCHA / login wall (scraper returns {"__blocked__": "captcha"}) => status failed, reason
    # captcha, so the report can say a CAPTCHA appeared.
    _patch_provider(monkeypatch, {"__blocked__": "captcha"})
    facts = collect_ai_visibility_facts(_settings(ai_visibility_enabled=True), domain="acme.test")
    assert facts["status"] == "failed"
    assert facts["reason"] == "captcha"


def test_collect_complete_path(monkeypatch) -> None:
    _patch_provider(monkeypatch, dict(_FULL_RAW))
    facts = collect_ai_visibility_facts(
        _settings(ai_visibility_enabled=True),
        domain="acme.test",
        retrieved_at="2026-07-16T00:00:00Z",
    )
    assert facts["status"] == "complete"
    assert facts["provider"] == "semrush"
    assert facts["domain"] == "acme.test"
    assert facts["retrieved_at"] == "2026-07-16T00:00:00Z"
    assert facts["visibility_score"] == 19
    assert len(facts["per_platform"]) == 3


# --- normalizer -----------------------------------------------------------------------------


def test_normalize_full_payload() -> None:
    facts = normalize_ai_visibility_facts(
        dict(_FULL_RAW), provider="semrush", domain="acme.test", retrieved_at="t"
    )
    assert facts["status"] == "complete"
    assert facts["mentions"] == 28
    assert facts["topics"][0]["ai_volume"] == "116.9K"


def test_normalize_clamps_visibility_score() -> None:
    facts = normalize_ai_visibility_facts(
        {"visibility_score": 130}, provider="p", domain="d", retrieved_at=None
    )
    assert facts["visibility_score"] == 100


def test_normalize_non_dict_is_empty_not_raise() -> None:
    for bad in ("nope", ["x"], 42, None):
        facts = normalize_ai_visibility_facts(bad, provider="p", domain="d", retrieved_at=None)
        assert facts["status"] == "empty"


def test_normalize_no_signal_is_empty() -> None:
    # A screenshot the model couldn't read (all null / no rows) must NOT fabricate a section.
    facts = normalize_ai_visibility_facts({}, provider="p", domain="d", retrieved_at=None)
    assert facts["status"] == "empty"
    assert facts["reason"] == "no_usable_data"


def test_normalize_drifted_key_degrades_to_empty() -> None:
    # extra="forbid" on the extraction model => an unexpected key degrades defensively, not a raise.
    facts = normalize_ai_visibility_facts(
        {"totally_unknown": 1}, provider="p", domain="d", retrieved_at=None
    )
    assert facts["status"] == "empty"


# --- report builder -------------------------------------------------------------------------


def test_report_builder_none_when_not_run() -> None:
    assert build_ai_visibility_report_data(None) is None


def test_report_builder_none_for_a_config_skip() -> None:
    # A CONFIG skip (disabled / no credentials) renders nothing — the operator has the feature off.
    assert build_ai_visibility_report_data({"status": "skipped", "reason": "x"}) is None


def test_report_builder_shows_a_note_when_the_run_found_nothing_readable() -> None:
    # "empty" means the bot DID reach Semrush and read nothing (e.g. the Multilogin interstitial
    # before it was detected). Silence there looks like a broken feature, so it must show a visible
    # note — with CLIENT-SAFE copy, since the section ships in the customer's PDF.
    section = build_ai_visibility_report_data({"status": "empty", "reason": "no_usable_data"})
    assert section is not None
    assert section["unavailable"] is True
    assert "No AI Visibility data is available" in section["message"]
    assert "Semrush" not in section["message"]


def test_report_builder_composes_section() -> None:
    facts = normalize_ai_visibility_facts(
        dict(_FULL_RAW), provider="semrush", domain="acme.test", retrieved_at="t"
    )
    section = build_ai_visibility_report_data(facts)
    assert section is not None
    assert section["visibility_score"] == 19
    assert section["visibility_band"] == "Low"
    # Metric tiles only for the present headline values.
    labels = {m["label"] for m in section["metrics"]}
    assert {"Mentions", "Citations", "Cited Pages", "Share of Voice"} <= labels
    # Percent display: whole numbers drop the ".0", fractionals keep one decimal.
    country = section["by_country"][0]
    assert country["share_display"] == "100%"
    platform = section["per_platform"][0]
    assert platform["share_display"] == "78.6%"


def test_report_builder_none_when_complete_but_no_data() -> None:
    # status complete but every panel empty => nothing renderable => no section.
    facts = {
        "status": "complete",
        "provider": "semrush",
        "visibility_score": None,
        "metrics": [],
        "per_platform": [],
        "topics": [],
        "competitors": [],
        "by_country": [],
    }
    assert build_ai_visibility_report_data(facts) is None


def test_report_builder_renders_captcha_note_for_failed_status() -> None:
    # A blocked run renders a VISIBLE "could not retrieve" note (not None), mentioning the CAPTCHA.
    facts = {"status": "failed", "reason": "captcha", "provider": "semrush", "domain": "acme.test"}
    section = build_ai_visibility_report_data(facts)
    assert section is not None
    assert section["unavailable"] is True
    assert "CAPTCHA" in section["message"]
    assert section["visibility_score"] is None
    assert section["metrics"] == []


def test_report_builder_renders_generic_note_for_failed_without_reason() -> None:
    section = build_ai_visibility_report_data({"status": "failed", "provider": "semrush"})
    assert section is not None
    assert section["unavailable"] is True
    assert "could not be retrieved" in section["message"]


# --- session-only safety (bot never types the password itself by default) -------------------


def test_bot_does_not_auto_login_without_session_by_default(tmp_path) -> None:
    # THE account-protection guarantee: with no saved session and auto-login OFF (the default), the
    # bot must NOT attempt a credential login (that's what trips CAPTCHAs / risks a lockout). It
    # returns a no_session marker WITHOUT launching a browser.
    from apps.worker.stages.ai_visibility.semrush_scraper import fetch_semrush_ai_visibility_sync

    settings = _settings(
        semrush_session_state_path=str(tmp_path / "does-not-exist.json"),
        semrush_allow_headless_login=False,
    )
    result = fetch_semrush_ai_visibility_sync(domain="acme.test", settings=settings)
    assert result == {"__blocked__": "no_session"}


def test_collect_no_session_marks_failed_with_reason(monkeypatch) -> None:
    _patch_provider(monkeypatch, {"__blocked__": "no_session"})
    facts = collect_ai_visibility_facts(_settings(ai_visibility_enabled=True), domain="acme.test")
    assert facts["status"] == "failed"
    assert facts["reason"] == "no_session"


def test_report_builder_tolerates_non_finite_share_pct() -> None:
    # A non-finite share_pct from the vision model must NOT crash the pure builder (which runs in
    # the render stage) — it renders as no-display, not an OverflowError that sinks the audit.
    facts = {
        "status": "complete",
        "provider": "semrush",
        "visibility_score": 19,
        "per_platform": [{"platform": "X", "mentions": 1, "share_pct": float("inf")}],
        "by_country": [{"country": "US", "mentions": 1, "share_pct": float("nan")}],
    }
    section = build_ai_visibility_report_data(facts)
    assert section is not None
    assert section["per_platform"][0]["share_display"] is None
    assert section["by_country"][0]["share_display"] is None


def test_report_builder_no_session_shows_connect_note() -> None:
    section = build_ai_visibility_report_data(
        {"status": "failed", "reason": "no_session", "provider": "semrush"}
    )
    assert section is not None
    assert section["unavailable"] is True
    assert "connect" in section["message"].lower()


# --- session lifetime: refresh on success, retry a STALE session -----------------------------
#
# Both paths drive the REAL `fetch_semrush_ai_visibility` against a fake Playwright, so the
# session-handling logic itself is under test (not a stubbed provider).


class _FakePage:
    """Minimal page whose logged-in state flips once `_login` runs (when the test allows it)."""

    def __init__(self, state: dict) -> None:
        self._state = state

    async def goto(self, url, **kw):
        self._state.setdefault("visited", []).append(url)

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def wait_for_selector(self, *a, **kw):
        return None

    async def wait_for_timeout(self, *a, **kw):
        return None

    async def screenshot(self, path, **kw):
        from pathlib import Path as _P

        _P(path).write_bytes(b"png")

    @property
    def url(self):
        return "https://www.semrush.com/ai-seo/overview/"

    def locator(self, selector):
        return _FakeLocator(0)

    def get_by_text(self, phrase):
        return _FakeLocator(0)


class _FakeLocator:
    def __init__(self, n: int) -> None:
        self._n = n
        self.first = self

    async def count(self):
        return self._n

    async def fill(self, *a, **kw):
        return None

    async def click(self, *a, **kw):
        return None


class _FakeContext:
    def __init__(self, state: dict) -> None:
        self._state = state

    def set_default_timeout(self, *a):
        return None

    def set_default_navigation_timeout(self, *a):
        return None

    async def new_page(self):
        return _FakePage(self._state)

    async def storage_state(self, path):
        from pathlib import Path as _P

        self._state["saved"] = self._state.get("saved", 0) + 1
        _P(path).write_text('{"cookies": ["refreshed"]}')


class _FakeBrowser:
    def __init__(self, state: dict) -> None:
        self._state = state

    async def new_context(self, **kw):
        self._state["context_kwargs"] = kw
        return _FakeContext(self._state)

    async def close(self):
        return None


def _install_fake_playwright(monkeypatch, state, *, blocks):
    """Patch the scraper's Playwright + vision + block detector."""
    from apps.worker.stages.ai_visibility import semrush_scraper as mod

    class _PW:
        def __init__(self):
            self.chromium = self

        async def launch(self, **kw):
            return _FakeBrowser(state)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _PW())

    async def _detect(page):
        return blocks.pop(0) if blocks else None

    monkeypatch.setattr(mod, "_detect_block", _detect)
    monkeypatch.setattr(
        mod,
        "extract_ai_visibility_from_images",
        lambda shots, domain, settings: type(
            "E", (), {"model_dump": lambda self: {"visibility_score": 42}}
        )(),
    )

    async def _login(page, settings):
        state["logins"] = state.get("logins", 0) + 1

    monkeypatch.setattr(mod, "_login", _login)


def test_successful_run_rewrites_the_session_so_it_does_not_age_out(tmp_path, monkeypatch) -> None:
    # Semrush rotates auth cookies as you browse. A successful scrape must write the state back,
    # otherwise the one-time connect snapshot slowly expires and the section silently disappears.
    from apps.worker.stages.ai_visibility.semrush_scraper import fetch_semrush_ai_visibility_sync

    session = tmp_path / "semrush_session.json"
    session.write_text('{"cookies": ["original"]}')
    state: dict = {}
    _install_fake_playwright(monkeypatch, state, blocks=[])

    settings = _settings(
        semrush_session_state_path=str(session),
        local_screenshot_storage_dir=str(tmp_path / "shots"),
    )
    result = fetch_semrush_ai_visibility_sync(domain="acme.test", settings=settings)

    assert result == {"visibility_score": 42}
    assert state["saved"] == 1
    assert "refreshed" in session.read_text()


def test_stale_session_retries_login_only_when_the_operator_opted_in(tmp_path, monkeypatch) -> None:
    # A session invalidated elsewhere (someone signed into the same account) reads as "no_session"
    # AFTER the dashboard load. With headless login OFF that must stay a block and never type the
    # password; with it ON the bot retries the login once and recovers.
    from apps.worker.stages.ai_visibility.semrush_scraper import fetch_semrush_ai_visibility_sync

    session = tmp_path / "semrush_session.json"

    session.write_text("{}")
    state: dict = {}
    _install_fake_playwright(monkeypatch, state, blocks=["no_session"])
    off = fetch_semrush_ai_visibility_sync(
        domain="acme.test",
        settings=_settings(
            semrush_session_state_path=str(session),
            semrush_allow_headless_login=False,
            local_screenshot_storage_dir=str(tmp_path / "shots"),
        ),
    )
    assert off == {"__blocked__": "no_session"}
    assert state.get("logins", 0) == 0, "must not type the password when opted out"

    session.write_text("{}")
    state = {}
    _install_fake_playwright(monkeypatch, state, blocks=["no_session", None, None])
    on = fetch_semrush_ai_visibility_sync(
        domain="acme.test",
        settings=_settings(
            semrush_session_state_path=str(session),
            semrush_allow_headless_login=True,
            semrush_email="a@b.test",
            semrush_password="pw",
            local_screenshot_storage_dir=str(tmp_path / "shots"),
        ),
    )
    assert on == {"visibility_score": 42}
    assert state["logins"] == 1


def test_stale_session_without_credentials_keeps_the_connect_note(tmp_path, monkeypatch) -> None:
    # Flag on but no password configured: the retry must be skipped so the report still says
    # "connect Semrush" (reason no_session) rather than degrading to a generic "unavailable".
    from apps.worker.stages.ai_visibility.semrush_scraper import fetch_semrush_ai_visibility_sync

    session = tmp_path / "semrush_session.json"
    session.write_text("{}")
    state: dict = {}
    _install_fake_playwright(monkeypatch, state, blocks=["no_session"])

    result = fetch_semrush_ai_visibility_sync(
        domain="acme.test",
        settings=_settings(
            semrush_session_state_path=str(session),
            semrush_allow_headless_login=True,
            semrush_email="",
            local_screenshot_storage_dir=str(tmp_path / "shots"),
        ),
    )
    assert result == {"__blocked__": "no_session"}
    assert state.get("logins", 0) == 0


def test_detect_block_flags_the_multilogin_interstitial() -> None:
    # Semrush parks an over-shared account on /multilogin/ — a valid cookie but no dashboard. It
    # slipped past the "/login" URL check in production and scraped as a silent "empty"; it must
    # read as no_session so the honest note (and the opt-in login retry) kick in.
    import asyncio

    from apps.worker.stages.ai_visibility.semrush_scraper import _detect_block

    class _Parked:
        url = "https://www.semrush.com/multilogin/?redirect_to=%2Fai-seo%2Foverview%2F"

        def get_by_text(self, phrase):
            return _FakeLocator(0)

        def locator(self, selector):
            return _FakeLocator(0)

    assert asyncio.run(_detect_block(_Parked())) == "no_session"
