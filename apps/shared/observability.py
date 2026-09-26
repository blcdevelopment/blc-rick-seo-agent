"""Optional Sentry error reporting for the API and Celery worker.

No-op unless ``SENTRY_DSN`` is configured (and ``sentry-sdk`` is installed), so local
dev / tests / the QA harness run untouched — mirroring the opt-in pattern used for Clerk
auth (empty issuer => disabled). Keeping the import lazy means the dependency is only
required when error reporting is actually turned on.
"""

from __future__ import annotations

import logging

from apps.shared.config import Settings

logger = logging.getLogger(__name__)

_initialized = False

# httpx logs every request's full URL at INFO ("HTTP Request: GET https://..."), and the worker
# runs at --loglevel=info with Celery owning the root logger — so any credential riding in a query
# string would land in `docker compose logs`. Providers send keys in headers; this keeps request
# URLs out of INFO logs as a backstop (warnings and errors still come through).
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def quiet_http_client_logs() -> None:
    """Raise the HTTP client loggers to WARNING. Idempotent; call once per process."""
    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def init_sentry(settings: Settings, component: str) -> bool:
    """Initialize Sentry if a DSN is configured. Returns True when enabled. Safe to call
    more than once; only the first call with a DSN initializes the SDK."""
    global _initialized
    if _initialized:
        return True

    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else ""
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error reporting off.")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
    )
    sentry_sdk.set_tag("component", component)
    _initialized = True
    logger.info("Sentry initialized for component=%s env=%s", component, settings.app_env)
    return True
