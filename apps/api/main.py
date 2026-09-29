from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from apps.api.routes import audits, google, health, metrics, shared
from apps.shared.config import Settings, get_settings
from apps.shared.observability import init_sentry, quiet_http_client_logs


def docs_enabled(settings: Settings) -> bool:
    """Interactive docs + the OpenAPI schema are a local-dev convenience. In production they would
    be public (Caddy forwards /api/* unauthenticated) and map the whole internal API, so they are
    served everywhere except APP_ENV=production."""
    return settings.app_env.strip().lower() != "production"


def create_app(settings: Settings) -> FastAPI:
    serve_docs = docs_enabled(settings)
    app = FastAPI(
        title="BLC Website Audit Automation",
        version="0.1.0",
        description="Local-first API for Phase 1 website audit jobs.",
        docs_url="/docs" if serve_docs else None,
        redoc_url="/redoc" if serve_docs else None,
        openapi_url="/openapi.json" if serve_docs else None,
        swagger_ui_parameters={
            "displayRequestDuration": True,
            "filter": True,
            "tryItOutEnabled": True,
        },
    )

    # The CORS spec forbids combining a wildcard origin with credentials. If "*" is ever
    # configured, disable credentials so the policy stays valid instead of reflecting an
    # any-origin-with-credentials response.
    cors_allow_credentials = "*" not in settings.api_cors_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(audits.router)
    # Create / poll / read an audit: open to anyone with PUBLIC_AUDITS_ENABLED (require_visitor).
    app.include_router(audits.visitor_router)
    # Search Console connect/OAuth routes exist only when the feature is on.
    if settings.search_console_enabled:
        app.include_router(google.router)
    # Public, token-gated report sharing — intentionally NOT behind Clerk auth.
    app.include_router(shared.router)

    if serve_docs:

        @app.get("/", include_in_schema=False)
        def redirect_to_swagger() -> RedirectResponse:
            return RedirectResponse(url="/docs")

    return app


settings = get_settings()
init_sentry(settings, component="api")
quiet_http_client_logs()
app = create_app(settings)
