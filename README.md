# BLC Website Audit Automation

Internal audit tool for **Builder Lead Converter**. Submit a prospect's website URL (optionally with
Instagram / Facebook / YouTube links) and it crawls the site, measures it, scores it against
versioned YAML rubrics, and renders a branded, client-ready PDF (plus DOCX).

**Live:** https://ai.builderleadconverter.com (Clerk sign-in required).

The scores are deterministic: rules over extracted facts, never a language model. An LLM only
rewrites prose on standalone social audits and reads the Semrush dashboard for the AI Visibility
section — it can never add, drop or invent a finding.

## Documentation

| Doc | Read it for |
|---|---|
| [docs/PRODUCT.md](docs/PRODUCT.md) | What the product is, the three audit types, what's built, what's deliberately not, the backlog |
| [docs/SETUP.md](docs/SETUP.md) | Local development setup and the quality gates |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, the pipeline stage by stage, data model, API surface, invariants |
| [docs/RUBRICS.md](docs/RUBRICS.md) | Scoring: rubric anatomy, evaluators, tuning, how findings are derived, how to defend the forecast |
| [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) | Running an audit and reading the report |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Production day-2: env changes, cron, Semrush connect, troubleshooting |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Honest caveats and accepted tradeoffs |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Deploy topology, first-time setup and CI/CD |

## Quickstart

```bash
conda env create -f environment.yml && conda activate social-audit   # native libs for WeasyPrint
make install        # poetry install --with dev
make browsers       # Playwright Chromium
cp .env.template .env

make docker-up      # postgres + redis + api (:8000) + worker
make run-frontend   # operator UI on :3000
```

Full walkthrough, configuration groups and troubleshooting: [docs/SETUP.md](docs/SETUP.md).

## Common commands

```bash
make test       # pytest (~493 tests, hermetic — never touches your real .env)
make lint       # ruff check .
make format     # ruff format .
make qa         # end-to-end QA against bundled fixtures; no infra, no API keys
make qa-repro   # runs the same site twice and asserts identical scores
make migrate    # alembic upgrade head
```

Maintenance scripts run directly (cron on the server):

```bash
python scripts/cleanup_storage.py [--dry-run]   # prune old reports/screenshots/exports
python scripts/health_alert.py [--dry-run]      # audit-health thresholds → ALERT_WEBHOOK_URL
```

⚠️ `scripts/run_social_audit.py`, `scripts/check_apify_social.py` and
`scripts/check_semrush_ai_visibility.py` call **paid or account-bound** services (Apify credits,
OpenAI vision, the shared Semrush session). Don't run them casually.

## Layout

```
apps/api/        FastAPI — audit endpoints, auth, public share links, metrics
apps/worker/     Celery worker — the pipeline lives in apps/worker/stages/
apps/shared/     Settings, SQLAlchemy models, lifecycle states, retention, observability
apps/frontend/   Next.js 14 operator UI (Pages Router, plain CSS)
rubrics/         Versioned YAML scoring rules — the tuning surface
prompts/         LLM prompts (social polish; the website prompts feed a dormant path)
templates/       Jinja2 + CSS for the PDF
migrations/      Alembic
scripts/         QA harness, cron jobs, live probes
tests/           Unit tests + HTML fixtures
```

## Configuration

Every setting lives in `.env.template` and is parsed by `apps/shared/config.py`. Everything external
is optional and degrades gracefully — no PageSpeed key, no Search Console, no social provider, no
Semrush: the audit still completes and the missing pieces never lower a score. With `CLERK_ISSUER`
empty the API is open, which is how local dev, tests and the QA harness run.

## Contributing

Work on a branch and open a PR — `pre-commit` (ruff, isort, flake8, the full pytest suite, frontend
typecheck) runs on every commit and again in CI, and merging to `main` auto-deploys to production.
