"""Session-wide isolation from the developer's real ``.env`` and shell credentials.

``apps.shared.config.Settings`` reads ``.env`` from the working directory, and the app builds its
cached settings at import time (``apps.api.main``, ``apps.shared.database``,
``apps.worker.celery_app``). Without this guard, a developer with real keys in ``.env`` would have
``pytest`` — and the pre-commit ``backend-tests`` hook, on every commit — make paid OpenAI /
Apify / Google Places calls, drive the Semrush login-bot (evicting the production session), report
to Sentry, 401 every TestClient request via ``CLERK_ISSUER``, or reach the configured database.

This module is imported before any test module (and so before any ``apps.*`` settings are built):

1. ``.env`` loading is switched off for every ``Settings()`` built during the session, so tests see
   the code defaults plus whatever they pass explicitly (init kwargs outrank everything).
2. Credentials and external-side-effect toggles are pinned empty/off in the process environment,
   which pydantic-settings ranks above defaults — so keys *exported in the shell* are neutralised
   too.
"""

from __future__ import annotations

import os

from apps.shared.config import Settings

Settings.model_config["env_file"] = None

_HERMETIC_ENV = {
    "APP_ENV": "test",
    # A real Postgres URL must never be reachable from a unit test; an accidental use of the real
    # get_db_session fails loudly ("no such table") instead of touching a live database.
    "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "AUDIT_ENQUEUE_ENABLED": "false",
    # Paid / quota'd providers and their credentials.
    "OPENAI_API_KEY": "",
    "GOOGLE_PSI_API_KEY": "",
    "APIFY_API_TOKEN": "",
    "YOUTUBE_API_KEY": "",
    "GOOGLE_PLACES_API_KEY": "",
    "FIRECRAWL_API_KEY": "",
    "BENCHMARK_ENABLED": "false",
    "BENCHMARK_API_KEY": "",
    "AI_VISIBILITY_ENABLED": "false",
    "SEMRUSH_EMAIL": "",
    "SEMRUSH_PASSWORD": "",
    "SEMRUSH_SESSION_STATE_PATH": "",
    "SEMRUSH_ALLOW_HEADLESS_LOGIN": "false",
    "GOOGLE_OAUTH_CLIENT_ID": "",
    "GOOGLE_OAUTH_CLIENT_SECRET": "",
    "YOUTUBE_ANALYTICS_CONNECT_ENABLED": "false",
    # Outbound reporting / alerting.
    "SENTRY_DSN": "",
    "ALERT_WEBHOOK_URL": "",
    # Auth is exercised explicitly in test_auth.py; an ambient issuer would 401 the API tests.
    "CLERK_ISSUER": "",
    "CLERK_AUTHORIZED_PARTIES": "",
    "CLERK_ALLOWED_SUBJECTS": "",
}

os.environ.update(_HERMETIC_ENV)
