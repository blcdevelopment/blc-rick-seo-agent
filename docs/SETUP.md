# Developer setup

Local development on macOS/Linux. Deploying to https://seo.builderleadconverter.com is in
[DEPLOYMENT.md](../DEPLOYMENT.md), and day-2 operations are in [OPERATIONS.md](OPERATIONS.md).

---

## 1. Prerequisites

| Tool | Why |
|---|---|
| Conda (Miniconda/Anaconda) | Supplies the native Pango / Cairo / GLib / gdk-pixbuf libraries WeasyPrint needs. **PDF rendering fails without them.** Poetry ships inside this env — no separate install |
| Node.js 18+ and npm | The UI (`apps/frontend`) |
| Docker + Docker Compose | Local PostgreSQL, Redis, API and worker |

```bash
conda env create -f environment.yml          # first time
conda env update -f environment.yml --prune  # later updates
conda activate social-audit

make install     # poetry install --with dev
make browsers    # playwright install chromium chromium-headless-shell
cp .env.template .env
```

`environment.yml` names the env `social-audit`, the same as the parent app (`blc-social-audit`), and
the Python package keeps the parent's name. Sharing one env works: tests, the QA harness and the
servers import `apps` from the checkout you run them in. But `make install` re-points the env's
editable install at whichever checkout ran it last. To keep the two apps fully apart, create a
separate env: `conda env create -f environment.yml -n rick-seo-agent`.

### The three dependency sources (read this once)

This is the most confusing thing about the repo:

- **`pyproject.toml`** is the contract. `make install` (Poetry) is the local dev path; CI installs
  the same graph with `pip install -e ".[dev]"`.
- **`poetry.lock`** pins local dev.
- **`requirements.txt`** is the pinned mirror the **Docker images** install (`uv pip install -r
  requirements.txt`, then `--no-deps -e .`).

If you change a dependency, update `pyproject.toml` **and** regenerate both `poetry.lock` and
`requirements.txt`. All three must stay in sync, or prod runs different versions than you tested.

## 2. Configuration

Every setting is documented in `.env.template` (a test enforces it) and parsed by
`apps/shared/config.py` from environment variables and the repo-root `.env`. `get_settings()` is
`lru_cache`d, so a process restart is needed to pick up a change.

| Group | Keys |
|---|---|
| Database | `DATABASE_URL`, `POSTGRES_*` |
| Queue | `REDIS_URL`, `CELERY_*` (the soft time limit must be below the hard limit) |
| Crawler | `CRAWLER_*` — page cap, timeouts, robots, private-host policy, SSRF interception |
| Storage | `LOCAL_REPORT_STORAGE_DIR`, `LOCAL_SCREENSHOT_STORAGE_DIR`, `LOCAL_TOOL_EXPORT_STORAGE_DIR` |
| Rubrics / prompts / templates | `RUBRIC_*`, `COMMENTARY_*`, `REPORT_TEMPLATE_PATH`, `REPORT_CSS_PATH`, `REPORT_SOCIAL_TEMPLATE_PATH`, `BRAND_CONFIG_PATH` |

### Rick edition settings

The code defaults reproduce the parent app; `.env.template` sets this edition's values.

| Key | Code default | `.env.template` | Effect |
|---|---|---|---|
| `REPORT_PROFILE` | `full` | `teaser` | `teaser`: every report surface shows problems and scores but no fixes, plus a booking call-to-action. **The API and the worker must use the same value** (the worker renders the PDF and DOCX when an audit completes; the API composes the JSON per request and re-renders a missing DOCX); files already generated are not re-rendered when it changes |
| `BOOKING_URL` | empty | empty | The call-to-action link. Must start with `https://`, `http://`, `mailto:` or `tel:` (otherwise settings fail to load); empty shows the label without a link |
| `BOOKING_CTA_LABEL` | `Book a meeting with Rick` | same | The call-to-action text |
| `PUBLIC_AUDITS_ENABLED` | `false` | `true` | Visitors create and read audits without signing in; operator endpoints keep the Clerk check, answer 403 without Clerk on any `APP_ENV` other than local/dev/test, and social-only audits are refused |
| `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED` | unset | `true` | The UI's copy of the flag, baked at build time — put it in `apps/frontend/.env.local` (or pass it as a Docker build arg). With it the UI renders without Clerk |
| `SEARCH_CONSOLE_ENABLED` | `true` | `false` | `false`: no Google calls, no `/google/search-console` routes, no Search Console blocks in any report. Its scored rules skip and the score rescales |

### Optional integrations

The app runs **fully functional with none of them**. Each degrades gracefully: a source that isn't
`complete` has its summary stripped before scoring, so a missing integration never penalizes a score
or aborts an audit.

| Group | Keys | Effect when empty |
|---|---|---|
| Clerk auth | `CLERK_ISSUER`, `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_AUTHORIZED_PARTIES`, `CLERK_ALLOWED_SUBJECTS` | **The API is open** — how local dev, tests and the QA harness run. Exception: with `PUBLIC_AUDITS_ENABLED` and any `APP_ENV` other than local/dev/test, operator endpoints answer 403. A public UI build needs no Clerk keys |
| PageSpeed | `GOOGLE_PSI_API_KEY`, `PSI_*` | PSI rules skip and the category rescales |
| Site-health sweep | `SITE_HEALTH_*` | On by default, no extra deps — the default technical crawl |
| Screaming Frog | `SCREAMING_FROG_*` | Off by default; when on it is preferred, with the sweep as fallback |
| Search Console | `SEARCH_CONSOLE_ENABLED`, `GOOGLE_OAUTH_*`, `GSC_*`, `URL_INSPECTION_MAX_URLS` | Off in this edition (`SEARCH_CONSOLE_ENABLED=false`). That also removes the Google connect flow, so `YOUTUBE_ANALYTICS_CONNECT_ENABLED` has nothing to attach to |
| Social providers | `APIFY_API_TOKEN`, `YOUTUBE_API_KEY`, `GOOGLE_PLACES_API_KEY` | That platform is skipped; a website audit is never promoted to combined |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` | Standalone-social prose falls back to its deterministic baseline and AI Visibility can't run. **Website commentary never calls an LLM either way** |
| AI Visibility | `AI_VISIBILITY_ENABLED`, `SEMRUSH_*` | Section omitted. When `true` it **auto-runs on every website/combined audit** and needs a saved Semrush session — see [OPERATIONS.md](OPERATIONS.md) |
| Sentry | `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE` | No-op |

## 3. Run it

```bash
make docker-up     # postgres + redis + api (runs `alembic upgrade head`) + worker → :8000
```

or natively, each in its own terminal (Postgres and Redis from `docker compose up -d postgres redis`):

```bash
make migrate
make run-api
make run-worker
make run-frontend  # :3000
```

The UI calls `http://localhost:8000` unless `NEXT_PUBLIC_API_BASE_URL` says otherwise, and the API's
`API_CORS_ORIGINS` must include the UI origin. Next.js reads env from `apps/frontend/.env.local`,
**not** the repo-root `.env`: put `NEXT_PUBLIC_API_BASE_URL` and
`NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true` there. Without the public flag the UI is Clerk-gated and
needs the Clerk publishable key to boot.

**Running next to the parent app on one machine.** The compose project is `blc-rick-seo-agent`, so
containers and volumes never mix with the parent's. Host ports still collide: give this edition its
own `POSTGRES_PORT` / `REDIS_PORT` in `.env` (and matching `DATABASE_URL`, `REDIS_URL`,
`CELERY_*`), never point it at the parent's Redis (its worker would take this app's jobs), and run
only one API on :8000 at a time.

`/docs`, `/redoc` and `/openapi.json` are served locally but **not when `APP_ENV=production`**.

## 4. Quality gates

```bash
make test      # pytest (~521 tests, ~45s)
make lint      # ruff check .
make format    # ruff format .
make qa        # hermetic end-to-end QA — no infra, no keys (add fixture=weak_site.html to vary)
make qa-repro  # same site twice; asserts identical scores
pre-commit install
```

`pre-commit` runs on every commit **and** in CI: whitespace/EOF/yaml/json/toml checks,
merge-conflict and large-file checks, `detect-private-key`, `no-commit-to-branch`, isort (black
profile), flake8, `ruff check --fix`, `ruff format`, **the full pytest suite**, and the frontend
`npm run typecheck` (locally only when files under `apps/frontend/` change; CI runs it always). New
Python must satisfy isort, flake8 and ruff — all three. That is also why a commit takes ~a minute.

**`tests/conftest.py` keeps the suite hermetic:** it switches off `.env` loading for every
`Settings()` in the session and pins credentials and paid / external-side-effect toggles, so
`pytest` (and the pre-commit hook) can never make a paid API call or drive the Semrush login bot
from your real `.env`. Never remove it, and never write a test whose settings depend on `.env`.
Unit tests therefore run with the code defaults (`full` profile, not public, Search Console on);
the edition behaviour is tested with explicit settings.

The QA harness (`make qa`) runs the real pipeline against bundled HTML fixtures over localhost with
an ephemeral SQLite DB and every external source forced onto its skip path — which is what makes
reproducibility testable. It follows `.env` for `REPORT_PROFILE` and `SEARCH_CONSOLE_ENABLED`
(the teaser, in this edition); run `REPORT_PROFILE=full make qa` to exercise the full report.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| WeasyPrint import / font errors | Native libs missing | `conda activate social-audit` |
| Crawler can't launch a browser | Playwright browser not installed | `make browsers`, or set `CRAWLER_CHROMIUM_EXECUTABLE_PATH` |
| `alembic upgrade head` fails on SQLite | Migrations target PostgreSQL (`pgcrypto`, `JSONB`) | Run against Postgres; SQLite is only ever built by `create_all` in tests/QA |
| `POST /audits` returns 503 | The job could not be queued (Redis/broker unreachable) | Start Redis. (A stopped worker does not 503 — the job just stays `queued`) |
| An audit sits at `queued` | No worker is consuming the queue | Start the Celery worker, pointed at the same `CELERY_BROKER_URL` as the API |
| 403 "Operator endpoints are disabled on this public deployment." | `PUBLIC_AUDITS_ENABLED` with no `CLERK_ISSUER` and an `APP_ENV` other than local/dev/test | Expected on a public deployment; set `APP_ENV=local` for local work |
| The UI still asks visitors to sign in | `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED` missing from `apps/frontend/.env.local`, or a stale build | Add it and restart `make run-frontend` (or rebuild the image) |
| Commentary provider says `deterministic` | That is the design, not a fallback | Nothing to fix — the website report never calls an LLM |
| A commit is rejected by `no-commit-to-branch` | You are on `main` | Work on a branch and open a PR |
