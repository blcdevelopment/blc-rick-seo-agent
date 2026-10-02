# BLC Website Audit Automation — Rick edition

A public audit site for **Builder Lead Converter**. A visitor submits their website URL without
signing in; the site crawls it, measures it, scores it against versioned YAML rubrics, and renders a
branded **teaser** report (web page, PDF and DOCX): the problems and scores, none of the fixes, and
a "Book a meeting with Rick" call-to-action. Social media is not part of this edition's audit.

**Status:** live at **https://seo.builderleadconverter.com** since 2026-09-29, on the parent's
shared box; how it runs and deploys is in [DEPLOYMENT.md](DEPLOYMENT.md). This repo is a separate copy of
`blcdevelopment/blc-social-audit` (the live app at https://ai.builderleadconverter.com). Nothing here may deploy to that app or share
its database, domain or compose project.

## Rick edition

The same pipeline and scoring engine as the parent; four settings (code defaults reproduce the
parent, `.env.template` sets this edition's values):

- **`REPORT_PROFILE=teaser`** keeps the findings (severity, what it means, why it matters, where it
  was found), evidence and scores, and strips every fix from the web page, PDF, DOCX and API JSON:
  "Do this" items, recommendations, the roadmap and tiers, technical "recommended fix" text,
  accessibility fix guidance, social remediation and LLM prose, and the summary's closing advice.
  A booking call-to-action takes their place (`BOOKING_URL`, `BOOKING_CTA_LABEL`). One module does
  it, `apps/worker/stages/report_profile.py`, as the last step of both report composers. Stored
  data and scores are untouched, and `REPORT_PROFILE=full` renders the parent's complete report.
  The API and the worker must run with the same value; PDFs/DOCX already rendered keep theirs.
- **`PUBLIC_AUDITS_ENABLED=true`** (API) plus **`NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true`** (UI build
  flag) let visitors run an audit and read its report without signing in. Operator endpoints
  (history, reruns, share links, metrics) keep the Clerk check; without Clerk they answer 403 on
  any `APP_ENV` other than local/dev/test. Public mode also refuses social-only audits and ignores
  white-label branding.
- **`SEARCH_CONSOLE_ENABLED=false`**: no Google calls, no OAuth routes, no Search Console blocks in
  any report. Its three scored checks are skipped, exactly as for a parent audit without a connected
  Google account.
- **`SOCIAL_AUDITS_ENABLED=false`** (API and worker) plus **`NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED=false`**
  (UI build flag): no social media in the audit. The form has no social fields, the API answers 422
  ("Social media audits are not available here.") to a request that carries social handles, and the
  worker never looks for the site's social links or calls Apify, YouTube, Google Places or OpenAI
  for social, for any job. Reports have no Social Media Audit section and no social or Overall
  Lead-Gen Readiness score; the headline is the website's Lead Generation Readiness. A social-only
  job queued before the switch fails with the same message; audits completed before it keep what
  they have. The social code stays in place (parity with the parent), switched off.

**AI Visibility** (`AI_VISIBILITY_ENABLED=true`, plus Semrush credentials and `OPENAI_API_KEY`)
replays a saved Semrush session (`storage/semrush_session.json`), exactly like the parent. A person
creates it once with `python scripts/check_semrush_ai_visibility.py --login`. Semrush allows one
live sign-in per account, so signing in here signs out the parent's bot. Without a working session
the teaser simply leaves the section out.

The scores are deterministic: rules over extracted facts, never a language model. An LLM only
rewrites prose on standalone social audits (never shown in the teaser, and switched off here) and
reads the Semrush dashboard for the AI Visibility section — it can never add, drop or invent a
finding.

## Documentation

| Doc | Read it for |
|---|---|
| [docs/PRODUCT.md](docs/PRODUCT.md) | What the product is, the three audit types, what's built, what's deliberately not, the backlog |
| [docs/SETUP.md](docs/SETUP.md) | Local development setup, the edition settings and the quality gates |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, the pipeline stage by stage, the report profile, data model, API surface and access rules, invariants |
| [docs/RUBRICS.md](docs/RUBRICS.md) | Scoring: rubric anatomy, evaluators, tuning, how findings are derived |
| [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) | Running an audit and reading the teaser report |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Day-2: settings, cron, the Semrush session, troubleshooting |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Honest caveats and accepted tradeoffs, including the public-mode gaps |
| [DEPLOYMENT.md](DEPLOYMENT.md) | The shared server (Part 1), how this app runs and deploys (Part 2), the box's rules, decisions, and the go-live record |

## Quickstart

```bash
conda env create -f environment.yml && conda activate social-audit   # native libs for WeasyPrint
make install        # poetry install --with dev
make browsers       # Playwright Chromium
cp .env.template .env                     # this edition's ports: Postgres :5434, Redis :6381
printf 'NEXT_PUBLIC_API_BASE_URL=http://localhost:8000\nNEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true\nNEXT_PUBLIC_SOCIAL_AUDITS_ENABLED=false\n' > apps/frontend/.env.local

docker compose up -d postgres redis   # compose project blc-rick-seo-agent
make migrate
make run-api        # :8000
make run-worker
make run-frontend   # public UI on :3000
```

The parent's local API also uses :8000, so run one app at a time. The conda env shares the parent's
name; [docs/SETUP.md](docs/SETUP.md) explains when to use a separate one, and has the full
walkthrough, configuration groups and troubleshooting.

## Common commands

```bash
make test       # pytest (~520 tests, hermetic — never touches your real .env)
make lint       # ruff check .
make format     # ruff format .
make qa         # end-to-end QA against bundled fixtures; no infra, no API keys
make qa-repro   # runs the same site twice and asserts identical scores
make migrate    # alembic upgrade head
```

Maintenance scripts run directly (on the box only the cleanup runs from cron;
[docs/OPERATIONS.md](docs/OPERATIONS.md) §4):

```bash
python scripts/cleanup_storage.py [--dry-run]   # prune old reports/screenshots/exports
python scripts/health_alert.py [--dry-run]      # audit-health thresholds → ALERT_WEBHOOK_URL
```

⚠️ `scripts/run_social_audit.py`, `scripts/check_apify_social.py` and
`scripts/check_semrush_ai_visibility.py` call **paid or account-bound** services (Apify credits,
OpenAI vision, the shared Semrush session). Don't run them casually.

## Layout

```
apps/api/        FastAPI — visitor + operator audit endpoints, auth, public share links, metrics
apps/worker/     Celery worker — the pipeline lives in apps/worker/stages/
apps/shared/     Settings, SQLAlchemy models, lifecycle states, retention, observability
apps/frontend/   Next.js 14 UI (Pages Router, plain CSS); public build via NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED
rubrics/         Versioned YAML scoring rules — the tuning surface
prompts/         LLM prompts (social polish; the website prompts feed a dormant path)
templates/       Jinja2 + CSS for the PDFs
migrations/      Alembic
scripts/         QA harness, cron jobs, live probes
tests/           Unit tests + HTML fixtures
```

## Configuration

Every setting lives in `.env.template` (a test enforces it) and is parsed by
`apps/shared/config.py`. Everything external is optional and degrades gracefully — no PageSpeed
key, no social provider, no Semrush: the audit still completes and the missing pieces never lower a
score. With `CLERK_ISSUER` empty the operator endpoints are open locally, which is how local dev,
tests and the QA harness run.

## Contributing

Work on a branch and open a PR — `pre-commit` (ruff, isort, flake8, the full pytest suite, and the
frontend typecheck when UI files change) runs on every commit and again in CI. Merging to `main`
does **not** deploy by itself: after the merge, run **Actions → Deploy → Run workflow** on `main`
([DEPLOYMENT.md](DEPLOYMENT.md), Part 2).
