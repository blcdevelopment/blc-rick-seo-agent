# Developer setup

Local development on macOS/Linux. Production deployment is [DEPLOYMENT.md](../DEPLOYMENT.md);
day-2 server operations are [OPERATIONS.md](OPERATIONS.md).

---

## 1. Prerequisites

| Tool | Why |
|---|---|
| Conda (Miniconda/Anaconda) | Supplies the native Pango / Cairo / GLib / gdk-pixbuf libraries WeasyPrint needs. **PDF rendering fails without them.** Poetry ships inside this env — no separate install |
| Node.js 18+ and npm | The operator UI (`apps/frontend`) |
| Docker + Docker Compose | Local PostgreSQL, Redis, API and worker |

```bash
conda env create -f environment.yml          # first time
conda env update -f environment.yml --prune  # later updates
conda activate social-audit

make install     # poetry install --with dev
make browsers    # playwright install chromium chromium-headless-shell
cp .env.template .env
```

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

Every setting is documented in `.env.template` and parsed by `apps/shared/config.py`
(environment-only; `get_settings()` is `lru_cache`d, so a process restart is needed to pick up a
change).

| Group | Keys |
|---|---|
| Database | `DATABASE_URL`, `POSTGRES_*` |
| Queue | `REDIS_URL`, `CELERY_*` (the soft time limit must be below the hard limit) |
| Crawler | `CRAWLER_*` — page cap, timeouts, robots, private-host policy, SSRF interception |
| Storage | `LOCAL_REPORT_STORAGE_DIR`, `LOCAL_SCREENSHOT_STORAGE_DIR`, `LOCAL_TOOL_EXPORT_STORAGE_DIR` |
| Rubrics / prompts / templates | `RUBRIC_*`, `COMMENTARY_*`, `REPORT_*`, `BRAND_CONFIG_PATH` |

### Optional integrations — all off by default

The app runs **fully functional with none of them**. Each degrades gracefully: a source that isn't
`complete` has its summary stripped before scoring, so a missing integration never penalizes a score
or aborts an audit.

| Group | Keys | Effect when empty |
|---|---|---|
| Clerk auth | `CLERK_ISSUER`, `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_AUTHORIZED_PARTIES`, `CLERK_ALLOWED_SUBJECTS` | **The API is open** — this is how local dev, tests and the QA harness run |
| PageSpeed | `GOOGLE_PSI_API_KEY`, `PSI_*` | PSI rules skip and the category rescales |
| Site-health sweep | `SITE_HEALTH_*` | On by default, no extra deps — the default technical crawl |
| Screaming Frog | `SCREAMING_FROG_*` | Off by default; when on it is preferred, with the sweep as fallback |
| Search Console | `GOOGLE_OAUTH_*`, `GSC_*`, `URL_INSPECTION_MAX_URLS` | The Search Console section reports no data |
| Social providers | `APIFY_API_TOKEN`, `YOUTUBE_API_KEY`, `GOOGLE_PLACES_API_KEY` | That platform is skipped; a website audit is never promoted to combined |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` | Standalone-social prose falls back to its deterministic baseline and AI Visibility can't run. **Website commentary never calls an LLM either way** |
| AI Visibility | `AI_VISIBILITY_ENABLED`, `SEMRUSH_*` | Section omitted. When `true` it **auto-runs on every website/combined audit** — see [OPERATIONS.md](OPERATIONS.md) |
| Sentry | `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE` | No-op |

## 3. Run it

```bash
make docker-up     # postgres + redis + api (runs `alembic upgrade head`) + worker → :8000
# or natively, one per terminal:
make migrate && make run-api && make run-worker
make run-frontend  # :3000
```

The UI calls `http://localhost:8000` unless `NEXT_PUBLIC_API_BASE_URL` says otherwise, and the API's
`API_CORS_ORIGINS` must include the UI origin. Next.js reads env from `apps/frontend/.env.local`,
**not** the repo-root `.env` — the Clerk publishable key has to be there for `npm run dev` to boot.

`/docs`, `/redoc` and `/openapi.json` are served locally but **not when `APP_ENV=production`**.

## 4. Quality gates

```bash
make test      # pytest (~493 tests, ~40s)
make lint      # ruff check .
make format    # ruff format .
make qa        # hermetic end-to-end QA — no infra, no keys (add fixture=weak_site.html to vary)
make qa-repro  # same site twice; asserts identical scores
pre-commit install
```

`pre-commit` runs on every commit **and** in CI: whitespace/EOF/yaml/json/toml checks,
`detect-private-key`, `no-commit-to-branch`, isort (black profile), flake8, `ruff check --fix`,
`ruff format`, **the full pytest suite**, and the frontend `npm run typecheck`. New Python must
satisfy isort, flake8 and ruff — all three. That is also why a commit takes ~a minute.

**`tests/conftest.py` keeps the suite hermetic:** it switches off `.env` loading for every
`Settings()` in the session and pins credentials and external toggles empty, so `pytest` (and the
pre-commit hook) can never make a paid API call or drive the Semrush login bot from your real `.env`.
Never remove it, and never write a test whose settings depend on `.env`.

The QA harness (`make qa`) runs the real pipeline against bundled HTML fixtures over localhost with
an ephemeral SQLite DB and every external source forced onto its skip path — which is what makes
reproducibility testable.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| WeasyPrint import / font errors | Native libs missing | `conda activate social-audit` |
| Crawler can't launch a browser | Playwright browser not installed | `make browsers`, or set `CRAWLER_CHROMIUM_EXECUTABLE_PATH` |
| `alembic upgrade head` fails on SQLite | Migrations target PostgreSQL (`pgcrypto`, `JSONB`) | Run against Postgres; SQLite is only ever built by `create_all` in tests/QA |
| `POST /audits` returns 503 | Worker or Redis unreachable | Start Redis and the Celery worker |
| Commentary provider says `deterministic` | That is the design, not a fallback | Nothing to fix — the website report never calls an LLM |
| A commit is rejected by `no-commit-to-branch` | You are on `main` | Work on a branch and open a PR |
