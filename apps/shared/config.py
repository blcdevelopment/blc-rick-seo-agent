from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    app_name: str = "blc-website-audit"
    log_level: str = "info"

    # Optional Sentry error reporting (API + worker). Empty DSN => disabled
    # (local dev / tests / QA harness), mirroring the Clerk opt-in pattern.
    sentry_dsn: SecretStr | None = None
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0, le=1)

    # Operational alerting (scripts/health_alert.py, run from cron). Empty webhook =>
    # disabled. Posts a Slack/Discord/generic-webhook message when a threshold is breached.
    alert_webhook_url: SecretStr | None = None
    alert_failed_audits_threshold: int = Field(default=5, ge=1)
    alert_stuck_audit_minutes: int = Field(default=60, ge=5)
    # PostgreSQL backups (scripts/backup_db.py, run from cron) -> timestamped .sql.gz.
    backup_storage_dir: Path = Path("./storage/backups")
    backup_retention_days: int = Field(default=14, ge=0)
    pg_dump_path: str = "pg_dump"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    api_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # Clerk auth. The API verifies session tokens against this issuer's JWKS.
    # Empty clerk_issuer => API auth is DISABLED (local dev / tests / QA harness);
    # set it in production to require a valid Clerk session on the audit endpoints.
    clerk_issuer: str = ""
    clerk_authorized_parties: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Optional defense-in-depth allowlist of Clerk user ids (the token `sub`) permitted to use
    # the API. Empty (default) => any validly-signed token from clerk_issuer is accepted. Set it
    # to the comma-separated Clerk user ids of the 5-10 operators (visible in the Clerk dashboard)
    # so a stranger who self-registers on the Clerk instance still can't reach the audit endpoints.
    clerk_allowed_subjects: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Public audits (Rick edition): visitors run an audit and read its report WITHOUT signing in.
    # Opens only the visitor endpoints (create, status, detail, PDF, DOCX); the unguessable job
    # UUID in the report URL is the only key to a report, like a share link. Operator endpoints
    # (history list, reruns, share links, metrics, Search Console connect) keep the Clerk check;
    # without CLERK_ISSUER and with any APP_ENV but local/dev/test they answer 403 instead of
    # falling open. Public mode also refuses standalone social-only audits. The frontend's
    # matching build flag is NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED.
    public_audits_enabled: bool = False

    database_url: str = "postgresql+psycopg://blc:change-me-local@localhost:5432/blc_website_audit"

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"
    audit_enqueue_enabled: bool = True
    # Hard limit kills the worker process; soft limit raises inside the task so the
    # job can be marked FAILED instead of leaving a row stuck in a non-terminal state.
    # 30-minute hard / 29-minute soft ceiling. Large sites with Screaming Frog and
    # full PageSpeed need the headroom; the per-stage time budgets still keep a single
    # audit from running anywhere near this long on a normal site.
    celery_task_time_limit_seconds: int = Field(default=1800, ge=30)
    celery_task_soft_time_limit_seconds: int = Field(default=1740, ge=15)

    local_report_storage_dir: Path = Path("./storage/reports")
    local_screenshot_storage_dir: Path = Path("./storage/screenshots")
    local_tool_export_storage_dir: Path = Path("./storage/tool_exports")
    # Storage retention: scripts/cleanup_storage.py deletes generated reports,
    # screenshots, and tool exports older than this many days. There is no in-app
    # scheduler (run it from cron on the host); 0 disables cleanup (keep forever).
    storage_retention_days: int = Field(default=90, ge=0)
    # Read-only share links: how long a generated share token stays valid.
    share_link_ttl_days: int = Field(default=7, ge=1, le=365)

    # Report profile. "full" is the complete report: findings AND their fixes ("Do this" action
    # items, recommendations, the roadmap). "teaser" (Rick edition) shows the problems (findings,
    # severity, evidence, scores), strips every fix, and shows a booking call-to-action in their
    # place. Applied once, where the report payload is composed, so the PDF, DOCX, API/share JSON
    # and UI always agree; the API and worker must therefore run with the same value.
    report_profile: Literal["full", "teaser"] = "full"
    # The teaser's call-to-action (unused by the full profile). Empty URL => the CTA renders its
    # label and message without a link.
    booking_url: str = ""
    booking_cta_label: str = "Book a meeting with Rick"

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o"
    openai_max_tokens: int = Field(default=4096, ge=512)
    openai_temperature: float = Field(default=0.2, ge=0, le=1)
    openai_timeout_seconds: int = Field(default=60, ge=1)

    # Cap on the deterministic content plan's findings. Recommendations pair 1:1 with the
    # rendered findings (a finding without its fix reads as unanswered), so this is the only
    # truncation knob — see content_plan._build_section.
    commentary_max_findings_per_section: int = Field(default=5, ge=1, le=20)

    google_psi_api_key: SecretStr | None = None
    psi_scope: Literal["homepage", "all_crawled_pages"] = "all_crawled_pages"
    psi_max_pages: int = Field(default=10, ge=1, le=50)
    psi_timeout_seconds: int = Field(default=60, ge=1)
    psi_max_retries: int = Field(default=3, ge=1)
    psi_cache_ttl_seconds: int = Field(default=86400, ge=0)
    # Total wall-clock budget for the PageSpeed stage. PSI runs serially over each
    # crawled page (mobile + desktop); without a budget a slow or rate-limited API can
    # push the whole audit past the Celery soft time limit. When the budget is reached
    # the stage stops issuing new requests and returns the pages collected so far.
    psi_total_budget_seconds: int = Field(default=360, ge=30)

    screaming_frog_enabled: bool = False
    screaming_frog_binary: Path | None = None
    screaming_frog_output_dir: Path = Path("./storage/tool_exports/screaming_frog")
    # Clamped at runtime under the Celery soft time limit (screaming_frog.py)
    # so a slow Screaming Frog crawl degrades gracefully instead of killing the
    # whole audit task.
    screaming_frog_timeout_seconds: int = Field(default=600, ge=30)
    screaming_frog_export_tabs: str = "Internal:All,Response Codes:Client Error (4xx)"

    # Built-in site health sweep (httpx status checks over discovered URLs +
    # sitemap). Runs when Screaming Frog is disabled or unavailable, so the
    # Technical SEO report section works in Docker/production without a
    # licensed desktop tool.
    site_health_enabled: bool = True
    site_health_max_internal_urls: int = Field(default=150, ge=1, le=1000)
    site_health_max_external_urls: int = Field(default=50, ge=0, le=500)
    site_health_check_external_links: bool = True
    site_health_concurrency: int = Field(default=8, ge=1, le=20)
    site_health_request_timeout_seconds: int = Field(default=10, ge=1, le=60)
    site_health_total_budget_seconds: int = Field(default=180, ge=10)
    site_health_sitemap_max_urls: int = Field(default=500, ge=0, le=5000)
    # Politeness: the sweep hits ONE host, so it must behave like a considerate crawler
    # (~1-2 req/s), or WAFs rate-limit it and dead-link findings become false positives.
    site_health_per_host_concurrency: int = Field(default=2, ge=1, le=8)
    site_health_request_delay_ms: int = Field(default=750, ge=0, le=5000)
    # Circuit breaker: after this many CONSECUTIVE internal network failures the sweep
    # stops and reports itself bot-blocked instead of emitting mass false "dead links".
    site_health_breaker_threshold: int = Field(default=5, ge=2, le=50)

    # Google Search Console: the external-SEO stage's search analytics + URL inspection, the
    # report's Search Performance blocks, and the /google/search-console connect routes. False
    # (the Rick edition) never contacts Google, mounts no OAuth routes and hides every Search
    # Console block on the PDF, DOCX and UI. Its scored rules skip and the score rescales, exactly
    # as for an audit with no connected Google account.
    search_console_enabled: bool = True

    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr | None = None
    # Signs the OAuth `state` parameter (CSRF protection). Optional: when empty,
    # each API process generates an ephemeral secret at startup, which is fine for
    # a single-process deployment; set it explicitly if the API runs replicated.
    google_oauth_state_secret: SecretStr | None = None
    google_oauth_redirect_uri: str = "http://localhost:8000/google/search-console/callback"
    google_oauth_success_redirect_url: str = "http://localhost:3000/audits"
    gsc_default_date_range_days: int = Field(default=90, ge=7, le=540)
    gsc_row_limit: int = Field(default=25000, ge=1, le=25000)
    # GSC "ranking opportunity" forecast knobs (google_search_console._opportunity_estimate).
    # Both applied values are recorded in the stored estimate (aio_prevalence_pct /
    # cap_multiple), so env tuning stays self-describing; the CTR curve and the 50/70/100%
    # capture ladder remain versioned constants (the model's identity, cited in prose).
    gsc_opportunity_aio_prevalence: float = Field(default=0.40, ge=0, le=1)
    gsc_opportunity_cap_multiple: float = Field(default=3.0, ge=1)
    url_inspection_max_urls: int = Field(default=20, ge=0, le=200)

    # Social data provider (Apify) — powers the standalone social audit (free-tier credits)
    # via the Instagram + Facebook actors. Empty token => the social collector skips, like
    # other optional external sources.
    apify_api_token: SecretStr | None = None
    apify_timeout_seconds: int = Field(default=120, ge=10)

    # YouTube Data API v3 — free public-data backend for the social audit (channel stats +
    # recent uploads). A plain API key (no OAuth); empty key => the YouTube collector skips
    # gracefully, like the Apify backends.
    youtube_api_key: SecretStr | None = None
    youtube_timeout_seconds: int = Field(default=30, ge=5)

    # Google Places API (New) — public Google Business Profile data (address, phone, category,
    # rating, review count, website) for the combined audit's business-identity enrichment
    # (SAE-12/13). A plain API key (no owner consent). Empty key => the Places enrichment skips
    # gracefully, like the Apify/YouTube backends; a website audit stays byte-identical.
    google_places_api_key: SecretStr | None = None
    google_places_timeout_seconds: int = Field(default=30, ge=5)

    # Connected-mode YouTube analytics (SAE-15, Wave 3): when True, the Google OAuth consent also
    # requests the owner-only YouTube Analytics scopes so a connecting channel owner can grant
    # private-metrics access (watch time, retention, demographics). Default False => the Google
    # OAuth grant is unchanged (Search Console only) and no connected YouTube data is fetched.
    youtube_analytics_connect_enabled: bool = False

    # Auto-discover social profile links (Instagram/Facebook/YouTube) from the crawled HTML to fill
    # any platform the operator left blank (explicit handles always win per platform — the
    # per-platform blank-fill the new-audit form promises). For a plain WEBSITE submission this
    # promotes it to a combined audit (appending the Social Media Audit + Overall Lead-Gen
    # Readiness sections), but ONLY when a matching provider credential (APIFY_API_TOKEN /
    # YOUTUBE_API_KEY) is configured — without those keys a website audit stays website-only and
    # the flag has no effect. Discovery is pure HTML parsing (no extra network), so audits stay
    # reproducible. Default ON; set false to stop the extra Apify/YouTube scrapes (conserve
    # free-tier credits) — a kill-switch, the audit still completes.
    social_autodiscovery_enabled: bool = True

    # Competitor benchmarking (P2-26 / SMWA-79 — Epic P2-E5 Enrichment, v3). Presents the audited
    # scores relative to competitor / industry baselines from a paid provider (SEMrush / Ahrefs /
    # Similarweb — no free reliable source). DEFERRED: OFF by default, and there is no live vendor
    # client yet, so the benchmark collector SKIPS gracefully unless (a) `benchmark_enabled` is set,
    # (b) `benchmark_provider` names a supported vendor, and (c) `benchmark_api_key` is configured —
    # and even then it no-ops until the vendor client is built and its recurring cost approved (the
    # acceptance gate). Until then the report is byte-identical and no cost is incurred (mirrors the
    # Apify / PSI / GSC missing-key skip pattern). Benchmarking is presentation only — it never
    # changes a score.
    benchmark_enabled: bool = False
    benchmark_provider: str = ""  # "" | "semrush" | "ahrefs" | "similarweb"
    benchmark_api_key: SecretStr | None = None

    # AI Visibility enrichment (Semrush AI Visibility Toolkit): an optional report section showing
    # how the brand appears in AI answers. When enabled it AUTO-RUNS on every website/combined
    # audit (tasks._augment_with_ai_visibility_safely, after the result commits) and can be re-run
    # on demand ("Refresh AI Visibility" -> POST /audits/{id}/rerun-ai-visibility, which the API
    # refuses with 409 while this is off — so flipping the kill-switch needs BOTH the api and the
    # worker restarted). Semrush has no API for this toolkit, so a Playwright bot replays the
    # operator's saved Semrush session, screenshots the dashboard, and an OpenAI vision model reads
    # it. Presentation only — it NEVER feeds scoring, so scores stay reproducible whether it ran or
    # not. OFF by default; skips gracefully unless enabled AND (a saved session OR SEMRUSH_EMAIL)
    # AND OPENAI_API_KEY are configured. See docs/OPERATIONS.md §5 (the
    # ToS/compliance decision is the operator's — low-volume, single server-side account).
    ai_visibility_enabled: bool = False
    ai_visibility_provider: str = "semrush"  # "" | "semrush"
    # Vision model for reading the dashboard screenshot; empty => falls back to openai_model.
    ai_visibility_vision_model: str = ""
    ai_visibility_headless: bool = True
    ai_visibility_timeout_seconds: int = Field(default=90, ge=10)
    # Upper bound (seconds) for waiting on the dashboard's score gauge to render after navigation;
    # the bot returns as soon as it appears (then a brief fixed settle for the async lower tables)
    # and only waits the full amount if the marker never shows. Too low => a slow page can be
    # screenshotted half-painted and extract as empty.
    ai_visibility_render_wait_seconds: int = Field(default=10, ge=0, le=120)
    # Semrush login. Prefer establishing the saved session once (probe --login, headed) so a human
    # clears any CAPTCHA/2FA; the bot reuses those cookies. Email+password is the headless fallback.
    semrush_email: str = ""
    semrush_password: SecretStr | None = None
    semrush_login_url: str = "https://www.semrush.com/login/"
    semrush_ai_visibility_url: str = "https://www.semrush.com/ai-seo/overview/"
    # Where the saved browser session (cookies) is stored; empty => never persist a session (always
    # log in fresh, which is more fragile). Gitignored under ./storage.
    semrush_session_state_path: str = "./storage/semrush_session.json"
    # SAFETY DEFAULT: the audit bot uses only the SAVED session and NEVER types the email/password
    # itself. A fresh headless credential login is exactly what trips Semrush's CAPTCHA and (worse)
    # can flag the account for "unusual activity" / force-logout on repeated attempts — so when
    # there's no valid session the bot shows a "connect Semrush" note instead of hammering the
    # login. Establish the session once (human-driven): `--login` locally, or `make semrush-connect`
    # on the headless server (view the browser over VNC). Set this True ONLY if you accept the
    # account risk and want the bot to attempt an automatic headless credential login.
    semrush_allow_headless_login: bool = False

    crawler_user_agent: str = "BLC-Audit-Bot/1.0 (+https://builderleadconverter.com/audit-bot)"
    crawler_max_pages: int = Field(default=10, ge=1, le=50)
    crawler_concurrency: int = Field(default=3, ge=1, le=10)
    crawler_page_timeout_seconds: int = Field(default=30, ge=1)
    crawler_respect_robots_txt: bool = True
    crawler_screenshots_enabled: bool = True
    crawler_allow_private_hosts: bool = False
    # Request-level SSRF guard: when enabled (default), every sub-resource/redirect the
    # headless browser tries to fetch during rendering is validated against the same
    # private/loopback/link-local/metadata-IP block-list as the page URL, closing the
    # mid-render SSRF gap. Auto-disabled when crawler_allow_private_hosts is true
    # (local dev / QA harness crawls against localhost fixtures).
    crawler_intercept_requests: bool = True
    crawler_chromium_executable_path: Path | None = None
    # Some hosts answer with a bot check (SiteGround, Cloudflare, AWS WAF, Vercel) before the page.
    # The crawler waits up to this many seconds for the site's own check to let the browser
    # through (it never solves or disguises anything), then fails the audit with a plain message
    # instead of scoring the check page. SiteGround's check gives up after 10 s. 0 = don't wait.
    crawler_challenge_wait_seconds: int = Field(default=15, ge=0, le=60)

    # Optional advisory accessibility pass (axe-core). Default OFF; mirrors
    # screaming_frog_enabled. When enabled, axe runs DURING the crawl (inside the live
    # page-render window, before the page closes) and its findings are stored separately
    # and rendered as an ADVISORY report section. The results NEVER feed the deterministic
    # scoring path (score_audit reads only seo/uxui/psi/external_seo), so scores stay
    # byte-for-byte reproducible whether this is on or off. When disabled, or if axe.min.js
    # is missing / the scan errors / it times out, the audit completes normally with no
    # advisory section (graceful skip, like a missing PSI key).
    accessibility_advisory_enabled: bool = False
    accessibility_axe_script_path: Path = Path("./vendor/axe-core/axe.min.js")
    accessibility_axe_timeout_seconds: int = Field(default=30, ge=5, le=120)
    accessibility_max_examples_per_issue: int = Field(default=5, ge=1, le=50)
    # color-contrast is the most expensive and least reproducible axe rule (it reads
    # computed styles); ops can disable it while keeping the structural advisory checks.
    accessibility_run_contrast: bool = True

    rubric_seo_path: Path = Path("./rubrics/seo.yaml")
    rubric_uxui_path: Path = Path("./rubrics/uxui.yaml")
    rubric_composite_path: Path = Path("./rubrics/composite.yaml")
    rubric_social_path: Path = Path("./rubrics/social.yaml")
    # Combined-audit Overall Lead-Gen Readiness weights (website composite + social). Combined
    # audits only; website/social audits never load it.
    rubric_overall_path: Path = Path("./rubrics/overall.yaml")
    commentary_system_prompt_path: Path = Path("./prompts/commentary_system.md")
    commentary_user_prompt_path: Path = Path("./prompts/commentary_user.md")
    commentary_social_system_prompt_path: Path = Path("./prompts/commentary_social_system.md")
    commentary_social_user_prompt_path: Path = Path("./prompts/commentary_social_user.md")
    brand_config_path: Path = Path("./brand/blc.yaml")
    report_template_path: Path = Path("./templates/report.html")
    report_css_path: Path = Path("./templates/report.css")
    report_social_template_path: Path = Path("./templates/social_report.html")

    @field_validator(
        "api_cors_origins",
        "clerk_authorized_parties",
        "clerk_allowed_subjects",
        mode="before",
    )
    @classmethod
    def parse_csv_list(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("booking_url")
    @classmethod
    def validate_booking_url(cls, value: str) -> str:
        # Rendered as a link in the PDF, DOCX and UI, so only real link schemes are accepted.
        value = value.strip()
        if value and not value.startswith(("https://", "http://", "mailto:", "tel:")):
            raise ValueError("booking_url must start with https://, http://, mailto: or tel:")
        return value

    @field_validator("crawler_chromium_executable_path", "screaming_frog_binary", mode="before")
    @classmethod
    def parse_optional_path(cls, value: str | Path | None) -> str | Path | None:
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_celery_time_limits(self) -> "Settings":
        if self.celery_task_soft_time_limit_seconds >= self.celery_task_time_limit_seconds:
            raise ValueError(
                "celery_task_soft_time_limit_seconds must be less than "
                "celery_task_time_limit_seconds so the soft limit fires first."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
