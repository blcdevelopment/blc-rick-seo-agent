# Architecture & Code Guide

**Scope:** how the system is built — components, data flow, the data model, the non-negotiable
patterns, and a file-by-file code map. For *what* the product does see [PRODUCT.md](PRODUCT.md);
for scoring detail see [RUBRICS.md](RUBRICS.md). (Any `*.mmd` diagrams are local scratch files —
`*.mmd` is gitignored, so they are not in the repo.)

---

## 1. High-level shape

```text
Next.js UI (apps/frontend)
        |  HTTP / JSON (Clerk Bearer token / __session cookie; no token in the public build)
        v
FastAPI Backend (apps/api)  ───────────────►  PostgreSQL
        |  enqueue                              (audit_jobs, audit_results,
        v                                        google_search_console_connections)
Redis broker ──► Celery Worker (apps/worker)  ────────────────────────────────┘
                      │  run_collection_audit (tasks.py)
                      ▼
              Pipeline stages (apps/worker/stages):
              crawler → psi_client → extractor_seo / extractor_uxui
              → external_seo → scoring → commentary → grounding_validator
              → report_payload (last step: report_profile) → pdf_renderer / docx_renderer
                      │
                      ▼
              Local report storage (storage/reports/*.pdf)
```

The design separates **product risk** (crawl / score / commentary / PDF quality)
from **infrastructure risk** (hosting), so the local app is proven before any
production hosting work. Later work extended this spine without rewriting it (see
[`PRODUCT.md`](PRODUCT.md)).

**Three audit types share this spine.** A job's `audit_type` (`website` | `social` |
`combined`) selects what runs: a **website** audit is the SEO + UX/UI pipeline above; a
**standalone social** audit runs only the social provider/score/PDF path
(`apps/worker/stages/social/`); and a **combined** audit — created when social links are added
to the Website Audit form, or by auto-promotion when the crawled site links its own
profiles (credential-gated discovery; promoted **only when the social collection succeeds**) —
runs the **untouched** website pipeline first, then
appends a social section and an **Overall Lead-Gen Readiness** score to produce **one report**
(PDF *and* DOCX). The combined flow is the headline feature; see §6.1. The website
pipeline's scoring and report sections are byte-for-byte unchanged by it.

### 1.1 Rick edition switches

This repo is the Rick edition of `blc-social-audit`: the same pipeline and scores, plus four
settings (all in `apps/shared/config.py`; code defaults reproduce the parent app):

| Setting | Code default | This edition | Effect |
|---|---|---|---|
| `REPORT_PROFILE` | `full` | `teaser` | `teaser` strips every fix from every report surface and adds a booking call-to-action (`BOOKING_URL`, `BOOKING_CTA_LABEL`); see §5 |
| `PUBLIC_AUDITS_ENABLED` (+ `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED` for the UI build) | `false` | `true` | Visitors create and read audits without signing in; operator endpoints stay gated; see §5 and §7 |
| `SEARCH_CONSOLE_ENABLED` | `true` | `false` | No Google calls, no `/google/search-console` routes, no Search Console blocks in any report |
| `SOCIAL_AUDITS_ENABLED` (+ `NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED` for the UI build) | `true` | `false` | No social part in any audit: the form has no social fields, `POST /audits` answers 422 to social handles, and the worker skips social discovery and collection for every job (`_run_social_pipeline` fails a queued social-only job; `_augment_with_social` never runs), so reports have no social section and no social or overall score. Audits completed before the switch keep theirs |

---

## 2. Components

| Component | Module | Responsibility |
|---|---|---|
| UI | `apps/frontend` | Submit URL, poll progress, read the report, download PDF/DOCX. Clerk-gated by default; the public build (`NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED`, `lib/auth.ts`) renders without Clerk and hides the operator extras (history, share, reruns, white-label) |
| API | `apps/api/routes/audits.py` | `visitor_router` (create, status/detail, PDF/DOCX download) and the operator `router` (list, rerun-enrichment, rerun-ai-visibility, share-link mint/revoke) |
| Public share | `apps/api/routes/shared.py` | **Unauthenticated**, token-gated report payload + PDF (`/shared/{token}`) |
| Metrics | `apps/api/routes/metrics.py` | Clerk-gated `GET /metrics` (audit counts, throughput, storage) |
| Google routes | `apps/api/routes/google.py` | GSC OAuth connect / callback / properties; mounted only when `SEARCH_CONSOLE_ENABLED` |
| Auth | `apps/api/auth.py` | `require_user()` — Clerk JWT verification (opt-in via `CLERK_ISSUER`) for operator endpoints; `require_visitor()` — open when `PUBLIC_AUDITS_ENABLED`, else defers to `require_user()` |
| API health | `apps/api/routes/health.py` | `GET /health` |
| App + CORS | `apps/api/main.py` | `create_app()` factory: CORS, routers, Swagger redirect; docs/OpenAPI off when `APP_ENV=production` |
| Settings | `apps/shared/config.py` | Env-driven `Settings` (single source of config) |
| Models | `apps/shared/models.py` | `AuditJob`, `AuditResult`, `GoogleSearchConsoleConnection` (+ portable `GUID`/JSON types) |
| Lifecycle | `apps/shared/audit_states.py` | `AuditStatus` enum + terminal states |
| DB session | `apps/shared/database.py` | SQLAlchemy engine + `SessionLocal` |
| Worker app | `apps/worker/celery_app.py` | Celery configuration (Redis broker/backend) |
| Orchestrator | `apps/worker/tasks.py` | `run_collection_audit` (branches on `audit_type`; `_augment_with_social` for combined/promoted, then the best-effort `_augment_with_benchmark_safely` and `_augment_with_ai_visibility_safely`) + `rerun_external_enrichment_for_audit` + `rerun_ai_visibility_for_audit` drive stages + status updates |
| Social audit | `apps/worker/stages/social/` | Provider adapters + registry, typed schema, collector, site-link auto-discovery (`discovery.py`), deterministic extractor/scorer/report builder (standalone *and* combined section) |
| Crawler | `apps/worker/stages/crawler.py` | Playwright render, link discovery, robots, SSRF guards |
| Blocked-site fallback | `apps/worker/stages/firecrawl_fallback.py` | Only when the site's security blocks the homepage: the same pages fetched through Firecrawl, built into the same `CrawledPage`s (off without `FIRECRAWL_API_KEY`) |
| PageSpeed | `apps/worker/stages/psi_client.py` | PSI mobile/desktop collection, retries, cache, graceful skip |
| Extractors | `extractor_seo.py`, `extractor_uxui.py` | Deterministic SEO / UX facts |
| External SEO | `external_seo.py`, `site_health.py`, `screaming_frog.py`, `google_search_console.py` | Technical-crawl sweep (+ optional Screaming Frog CLI) and GSC facts (skipped with reason `disabled` when `SEARCH_CONSOLE_ENABLED=false`); always degrades gracefully |
| Scoring | `apps/worker/stages/scoring.py` | YAML rubric engine → SEO/UX/Lead-Gen scores |
| Commentary | `apps/worker/stages/commentary.py`, `content_plan.py` | Deterministic content plan; the website LLM polish is dormant scaffolding (the standalone-social polish is live — §5) |
| Grounding | `apps/worker/stages/grounding_validator.py` | Strip unsupported numeric claims |
| Report payload | `apps/worker/stages/report_payload.py` | Compose the report data model; its last step applies the report profile |
| Report profile | `apps/worker/stages/report_profile.py` | `apply_report_profile` / `apply_social_report_profile`, the last step of both composers: in `teaser` mode strips every fix and adds the booking call-to-action (§5) |
| Branding | `apps/worker/stages/report_branding.py` | BLC brand config + placeholder fallback |
| PDF renderer | `apps/worker/stages/pdf_renderer.py` | WeasyPrint/Jinja2 branded PDF → `storage/reports/` |
| DOCX renderer | `apps/worker/stages/docx_renderer.py` | Hand-written OOXML DOCX (failure never aborts the audit) |

**Versioned assets** (tunable without code): `rubrics/*.yaml`, `prompts/*.md`,
`templates/report.html` + `report.css`, `templates/social_report.html`, `brand/blc.yaml`. See
[`RUBRICS.md`](RUBRICS.md) for rubric structure and tuning.

---

## 3. Audit lifecycle (states)

`AuditStatus` (`apps/shared/audit_states.py`) drives the progress the UI shows:

```text
queued → crawling → collecting_performance → extracting → scoring
       → commenting → validating → rendering → complete
                                              (or → failed)
```

`tasks.py` updates `status`, `current_stage`, and `progress_pct` on the
`audit_jobs` row at each transition. The full progression is:

```text
15  crawling
45  collecting_performance (PSI)
70  extracting (SEO + UX/UI)
76  extracting (external SEO — technical-crawl sweep, + GSC when SEARCH_CONSOLE_ENABLED)
80  scoring
88  commenting
95  validating
96  rendering  "Auditing social profiles"      (only when social handles resolve — see below;
                                               never while SOCIAL_AUDITS_ENABLED=false)
97  rendering  "Collecting AI visibility"      (only when AI_VISIBILITY_ENABLED=true)
98  rendering
100 complete   (or → failed)
```

For a **combined** audit the website stages run identically, then a social add-on step
("Auditing social profiles", pct **96**) collects + scores the social profiles and computes the
Overall Lead-Gen Readiness before the same RENDERING stage (98) emits one combined report. That
step is **graceful**: any failure in the social/overall work (missing `overall.yaml`, bad
provider data, …) is caught and the audit still completes as a **website-only** report — it never
fails the whole combined job. An auto-discovered promotion is further gated on success: a plain
website submission is flipped to `combined` only when the collection produced usable data, so a
failed fetch leaves the website report byte-identical (no hollow social section). Social findings
in a combined report are deterministic (no LLM).

`_mark_job` is the worker's single writer of job state: it commits each transition, clears
`error_message` on a success transition, and sets `started_at`/`completed_at`. (The API also
writes `status` directly in `routes/audits.py` — FAILED on an enqueue failure, EXTRACTING when it
queues an enrichment rerun, COMPLETE when that queueing fails — so a lifecycle change must cover
both places.) On any
exception the transaction is rolled back, the job is marked `failed` with the error
message, and the exception is re-raised so Celery records the failure
(`SoftTimeLimitExceeded` is always re-raised).

**Re-enrichment path.** An already-`complete` audit can be re-run for *external SEO
only* via the `rerun_external_enrichment` Celery task (orchestrated by
`rerun_external_enrichment_for_audit`): it re-collects external SEO → rescores (pct 82)
→ re-comments → re-renders, **without** re-crawling or re-running PSI. It snapshots the
result fields first and restores them — keeping the job `complete` with the prior
report — if the rerun fails. Exposed via `POST /audits/{job_id}/rerun-enrichment`.
It is **combined-aware**: the website-only rescore drops every add-on key, so it carries forward
**every** stored `score_breakdown` key it didn't produce (`social`, `benchmark`, `ai_visibility`,
…) and recomputes `overall_readiness` from the freshly re-scored website Lead-Gen + the stored
Social Score — only when the audit has a Social Score or already carried `overall_readiness`, so a
degraded combined job stays website-only.

**AI-visibility refresh.** `POST /audits/{job_id}/rerun-ai-visibility` (409 while
`AI_VISIBILITY_ENABLED` is off) queues `rerun_ai_visibility_for_audit`, which re-runs only the
Semrush + vision step and re-renders, restoring the snapshot on failure (the audit stays
`complete`). The same step also runs automatically at 97% on every website/combined audit while
the flag is on.

---

## 4. Data model

| Table | Key fields |
|---|---|
| `audit_jobs` | `id`, `url`, `niche`, `target_audience`, `status`, `current_stage`, `progress_pct`, `error_message`, `audit_type` (free `String(20)`: `website`/`social`/`combined`), `social_handles` (JSON), `brand_overrides` (JSON), `share_token` + `share_expires_at`, timestamps |
| `audit_results` | `job_id` (1:1, CASCADE, unique), `seo_score`, `uxui_score`, `lead_gen_score` (all NULLABLE — empty for a social audit), `social_score`, plus JSON blobs: `crawled_pages`, `seo_facts`, `uxui_facts`, `psi_facts`, `external_seo_facts`, `social_facts`, `accessibility_facts` (advisory axe-core findings; nullable, never scored), `score_breakdown`, `commentary`, `validation_log`, `report_metadata`, `pdf_path`, `rubric_version`, `llm_model` |
| `google_search_console_connections` | Standalone (no FK to jobs/results), keyed by unique `account_email`; stores Google OAuth tokens (`access_token`, `refresh_token`, `token_expires_at`), `scopes` (JSON), `properties` (JSON), timestamps |

For a **combined** audit the social results are merged onto the **same** `audit_results` row as
the website result: `social_score` + `social_facts` are filled, and `score_breakdown` gains a
`"social"` key and an `"overall_readiness"` key. The Overall Lead-Gen Readiness number lives
**inside `score_breakdown` JSON** (`score_breakdown.overall_readiness.score`) — there is **no new
column** for it, and adding the `combined` type needed **no new migration** (`audit_type` is a
free string column; Alembic head stays `20260625_0005`).

JSON columns use PostgreSQL `JSONB` in production and portable `JSON` elsewhere; a
`GUID` type decorator maps to Postgres UUID or `CHAR(36)`. This portability is what
lets the hermetic QA harness run on SQLite. Migrations live in `migrations/`
(`alembic upgrade head`; head = `20260625_0005`, which adds the advisory
`accessibility_facts` column — see `migrations/versions/` for the full additive chain); the
Compose `api` service runs them on start. Alembic targets PostgreSQL only (`CREATE EXTENSION pgcrypto`,
`JSONB`); SQLite tables are created via `Base.metadata.create_all` in tests/QA, never
via Alembic.

### 4.1 The stage contract

Each stage produces a structured artifact the next stage consumes; the fact bundle
passed to scoring is
`{"seo": seo_facts, "uxui": uxui_facts, "psi": psi_facts, "external_seo": external_seo_facts}`,
and rubric rules reference facts by `fact_path` (e.g. `seo.summary.pages_with_schema`,
`external_seo.technical_crawl.summary.missing_titles`,
`uxui.pages[0].forms.total_field_count`). The unified external-SEO key is
`external_seo.technical_crawl.*` (the legacy `external_seo.screaming_frog.*` key is still
read for backward compat). The social audit reuses this same rubric-engine seam with its own
`{"social": social_facts}` bundle scored against `social.yaml` (standalone, **not** folded into
the website composite — see §6).

---

## 5. Key design decisions (non-negotiable)

- **Scores are deterministic and rule-based.** Commentary never produces a score.
  Identical facts always yield identical scores.
- **Website commentary is fully deterministic.** `commentary.py` builds its prose from
  the deterministic content plan (`content_plan.py`) and reports
  `status/provider/model == "deterministic"` — the website path never calls OpenAI (the
  `_call_openai()` website-polish function and `prompts/commentary_system.md` /
  `commentary_user.md` are dormant scaffolding with no caller). OpenAI **is** called live in two
  places when `OPENAI_API_KEY` is set: `_call_openai_social()` polishes the rule-derived findings
  of a **standalone** social audit (prompts `commentary_social_*.md`; grounded, falls back to the
  deterministic text on any failure), and `ai_visibility/vision.py` reads the Semrush dashboard
  screenshot when AI Visibility is enabled. In both, rules produce numbers and the LLM only
  produces prose or reads a picture — never invert this. In `teaser` mode the social polish still
  runs and is stored, but the report shows the deterministic summary and no narratives.
- **Grounded commentary.** Numeric claims in commentary are checked against the
  extracted facts; unsupported claims are stripped (`grounding_validator.py`). Timeframe
  phrases ("1–3 months") are masked first so they survive, and if stripping would empty a
  field it reverts to baseline prose.
- **Config-driven rubrics.** Scoring rules live in external YAML, not in code, and
  are versioned (bump the version when you tune — see [`RUBRICS.md`](RUBRICS.md)).
- **Structured pipeline, not autonomous agents.** Each audit is a fixed
  Extract → Score → Commentate → Validate sequence. No free-form agent loops.
- **Graceful degradation.** Missing PSI keys, an absent/failed external-SEO source
  (Screaming Frog / GSC / site-health), failed internal pages, and missing performance
  data never abort an audit — they downgrade to fallbacks or skipped rules. Only
  `status == "complete"` external-SEO summaries are scored; non-complete sources have
  their summary stripped before scoring (`scoring._trusted_external_seo_facts`).
- **Config is environment-driven.** Settings come from environment variables and `./.env`
  (`apps/shared/config.py`, every field documented in `.env.template` — a test enforces it).
- **The report profile is a rendering mode, applied in one place.** `report_profile.py` runs as
  the last step of `compose_report_payload` and `compose_social_report_payload`, so the PDF, DOCX,
  API detail JSON, share link and UI read the same payload. In `teaser` mode it strips findings'
  action items, tiers and "Start by checking" labels, recommendations, the roadmap, technical-SEO
  `recommended_fix`, axe `help_url`/`failure_summary`, social remediation/narratives/roadmap (and an
  LLM social summary), the executive-summary closing advice, and an AI Visibility block that
  could not collect; it adds `cta`. Scoring, stored facts and stored commentary are untouched, so
  remediation stays in the database and `full` restores every fix on the next render. The worker
  renders the PDF (and the DOCX at completion) and the API composes the JSON per request, so both
  processes must run with the same `REPORT_PROFILE`; files already generated are not re-rendered
  when it changes.
- **Authentication is Clerk, opt-in by env; visitors can be anonymous.** `apps/api/auth.py`
  `require_user()` verifies a Clerk RS256 JWT (from the `Authorization: Bearer` header or the
  `__session` cookie) against the issuer's JWKS and guards the operator endpoints (history,
  reruns, share mint/revoke, `/metrics`, the Google routes except the unauthenticated GSC OAuth
  callback, which an HMAC-signed, time-limited CSRF state protects instead). It is **opt-in**: if
  `CLERK_ISSUER` is empty it returns `None` and those endpoints are open — how local dev, the QA
  harness and tests run — **except** with `PUBLIC_AUDITS_ENABLED` and any `APP_ENV` other than
  `local`/`dev`/`development`/`test`, where it answers 403 so operator endpoints never fall open on
  a public deployment. `require_visitor()`
  guards the visitor endpoints (create, status, detail, PDF, DOCX): open to anyone when
  `PUBLIC_AUDITS_ENABLED` (the unguessable job UUID is the only key to a report, like a share
  link), otherwise it defers to `require_user()`. An optional `clerk_allowed_subjects` allowlist further restricts
  which Clerk user IDs may call the API, and the `azp` (authorized-party) check is hardened so a
  token that simply omits the claim no longer slips past.
- **Reports are stored on the local filesystem** under `storage/reports/` (object storage
  was evaluated and removed by decision for the single internal VM; `cleanup_storage` prunes
  old artifacts from host cron).

---

## 6. Scoring & the Lead-Generation Readiness score

`scoring.py` is a pure, config-driven rubric engine:

- `load_rubric` validates each `rubrics/*.yaml` (Pydantic, `extra="forbid"`). Today:
  `seo.yaml` (`phase2-seo-v12`, 48 rules — covering on-page SEO, JSON-LD/schema, CrUX Core Web
  Vitals, canonicals + redirect chains, HTTPS + mixed content, answer-engine (AEO) structure,
  local SEO (NAP/service-area/GBP/address), and static-HTML accessibility
  rules; v12 moved the presentation-level finding-merge pairs into per-rule `merged_into`
  metadata — scoring unchanged), `uxui.yaml` (`phase2-uxui-v3`, 14 rules), `composite.yaml`
  (`phase1-composite-v1`, weights only). The combined `rubric_version` stored on the result is
  `phase2-seo-v12+phase2-uxui-v3+phase1-composite-v1`.
  - **Separate from scoring:** an **optional, opt-in axe-core advisory accessibility pass**
    (`accessibility.py`, `accessibility_advisory_enabled`, default off) runs in the live crawl
    browser and stores render-dependent findings (colour contrast, computed ARIA, …) in the
    `accessibility_facts` column, rendered as an advisory report section. It is **never passed to
    `score_audit`** — scores are byte-for-byte identical whether it ran or not.
- Each rule has a `weight`, a `fact_path`, and an `evaluator`
  (`boolean`, `presence`, `range`, `exact_match`, `threshold`, `linear_scale`),
  optionally `skip_if_missing` (used for PSI rules with `linear_scale` so a missing API
  key doesn't penalize — the fact is dropped from both numerator and denominator and the
  category rescales). `threshold` is overloaded: `min`/`partial_min` = higher-is-better;
  `max`/`partial_max` = lower-is-better (used for all external-crawl/GSC count rules).
- Each rule also carries content-plan metadata consumed by `content_plan.py`: `impact`,
  `tier`, `finding_label`, `remediation`, `surface_as_finding` (defaults `impact=medium`,
  `tier=quick_win`, `surface_as_finding=true`).
- `score_category` evaluates rules, rescales to `max_score`, and emits a per-rule
  audit trail.
- `compose_lead_generation_score` combines the category scores via
  `rubrics/composite.yaml` weights. The website composite is **0.45 SEO + 0.55 UX/UI**
  (weights must sum to 1.0 over exactly `{seo, uxui}` — a typed `Literal["seo","uxui"]` set).
  **`social` is deliberately NOT folded into this website composite.** Instead, a **separate**
  Overall Lead-Gen Readiness score blends website + social for combined audits (§6.1), leaving
  the website composite untouched.

Reproducibility is the whole point: same facts in → same scores out, with a visible
breakdown explaining every contribution.

### 6.1 Overall Lead-Gen Readiness (combined audits)

For a **combined** audit, `scoring.compose_overall_readiness_score()` blends the website
Lead-Gen composite (SEO + UX/UI) with the standalone Social Score into one 0–100 headline
number. It is **config-driven** via a new rubric file `rubrics/overall.yaml`
(version `phase2-overall-v1`; keys `version`, `max_score`, `website_weight` **0.70**,
`social_weight` **0.30**, validated to sum to 1.0 by a new `OverallRubric` Pydantic model),
located by the `rubric_overall_path` setting (default `./rubrics/overall.yaml`, documented in
`.env.template` as `RUBRIC_OVERALL_PATH`). The weighting rationale: the website is the
bottom-of-funnel lead-capture surface (forms/calls/high-intent search convert there) so it
dominates, while social is top-of-funnel demand-gen/nurture — secondary. When the social audit
produced no score the readiness **rescales to the website Lead-Gen score alone** (social weight
drops out). Half-up rounding, like the rest of the engine. The result is stored under
`score_breakdown.overall_readiness` (no new column).

---

## 7. API surface

| Endpoint | Purpose | Access |
|---|---|---|
| `GET /health` | Liveness | public |
| `GET /metrics` | Operational metrics (audit counts by status, 24h throughput, in-flight/oldest, storage usage) | operator |
| `GET /` | 307 redirect → `/docs` (404 in production, where the docs are off) | public |
| `POST /audits` | Create + enqueue an audit job (201). `audit_type` ∈ `website`/`social`/`combined`; a combined audit requires **both** `url` and ≥1 social handle. In public mode `brand_overrides` is ignored and a social-only audit is refused (422) | visitor |
| `GET /audits` | List recent audits (`limit` 1–100, default 25; `offset`). Rows expose `audit_type` + a combined-only `overall_score` | operator |
| `GET /audits/{job_id}` | Audit detail + composed report payload, shaped by the report profile (a combined audit uses the website payload, which carries the appended sections); exposes `audit_type` + `overall_score` | visitor |
| `GET /audits/{job_id}/status` | Progress (stage, percentage, report availability) | visitor |
| `POST /audits/{job_id}/rerun-enrichment` | Re-run external SEO → rescore/recomment/re-render (404 no job / 409 no result / 503 enqueue fail) | operator |
| `POST /audits/{job_id}/rerun-ai-visibility` | Re-run only the Semrush AI-visibility step → re-render (409 while `AI_VISIBILITY_ENABLED` is off, checked first / 404 no job / 409 no result / 503 enqueue fail) | operator |
| `GET /audits/{job_id}/report` | Download the generated PDF | visitor |
| `GET /audits/{job_id}/docx` | Download the DOCX (rendered on demand if absent — this GET writes the file and commits its path, including for an anonymous visitor in public mode) | visitor |
| `POST /audits/{job_id}/share` | Mint a random, time-limited share token (`SHARE_LINK_TTL_DAYS`; 409 if no report yet) | operator |
| `DELETE /audits/{job_id}/share` | Revoke the share token | operator |
| `GET /shared/{token}` | Token-gated report payload, shaped by the report profile (404 missing/revoked, 410 expired) | public |
| `GET /shared/{token}/report` | Token-gated PDF download | public |
| `GET /google/search-console/connect` | Start GSC OAuth | operator; only when `SEARCH_CONSOLE_ENABLED` |
| `GET /google/search-console/connect-url` | Return the GSC OAuth URL | operator; only when `SEARCH_CONSOLE_ENABLED` |
| `GET /google/search-console/callback` | GSC OAuth callback (protected by an HMAC-signed CSRF state) | public; only when `SEARCH_CONSOLE_ENABLED` |
| `GET /google/search-console/properties` | List connected GSC properties | operator; only when `SEARCH_CONSOLE_ENABLED` |
| `GET /docs`, `GET /redoc`, `GET /openapi.json` | Interactive API docs — **disabled when `APP_ENV=production`** (they would be public via Caddy's `/api/*`) | public |

**Access:** *visitor* = `require_visitor` (open to anyone when `PUBLIC_AUDITS_ENABLED`, otherwise
Clerk); *operator* = `require_user` (Clerk when `CLERK_ISSUER` is set; open when it is empty, except
403 with `PUBLIC_AUDITS_ENABLED` and any `APP_ENV` but local/dev/test); *public* = never gated.

`compose_report_payload(job, result, *, settings=None)` (`apps/worker/stages/report_payload.py`,
`REPORT_PAYLOAD_VERSION` `phase1-report-v3`) has no I/O and is deterministic for a given
(job, result, settings); it is imported by both the worker (to render) and the API (to build the
detail and share responses), which call it without `settings`, so the process's own settings
(`REPORT_PROFILE`, `SEARCH_CONSOLE_ENABLED`) apply. Keep it free of I/O. The edition added three
fields: `report_profile` (`full`/`teaser`), `cta` (the booking call-to-action, teaser only) and
`show_search_console` (read by the PDF, DOCX and UI to hide every Search Console block; the
`seo.gsc.*` rules are also left out of reader-facing rule lists). `ReportPayload` also carries
two **optional** fields, `social_audit` and `overall_readiness` (both default `None` ⇒ not
rendered ⇒ a website-only report stays byte-identical); `compose_report_payload` populates them
from `result.social_facts` + `score_breakdown` for a combined audit, reusing the shared
deterministic builder `social/report.py::build_social_report_data` (refactored out of
`compose_social_report_payload` so the standalone social report and the combined social section
share one builder). The PDF template (`templates/report.html`) appends the two sections — TOC
entries + sections — at the **end**, skew-proof-guarded via `payload.get('social_audit')` /
`get('overall_readiness')`; `docx_renderer.py` appends the same via a new `_combined_xml()`
helper, so the on-demand DOCX matches the PDF. The standalone Social audit has its own seam in
`social/report.py` (`SOCIAL_REPORT_VERSION` `phase2-social-report-v1`):
`compose_social_report_data` builds the complete report (every fix included — the worker's
commentary step reads it, so stored data never depends on the profile), and
`compose_social_report_payload` applies the report profile on top; every surface uses the latter.

**Authentication** (see §5 and `apps/api/auth.py`): the Access column above. When
`CLERK_ISSUER` is set, operator endpoints require a verified Clerk JWT and the optional
`CLERK_ALLOWED_SUBJECTS` allowlist answers 403 for any other user; visitor endpoints require it
too unless `PUBLIC_AUDITS_ENABLED`. This edition has no Clerk instance of its own: visitors never
sign in, and without `CLERK_ISSUER` a public deployment closes the operator endpoints
(403). CORS has a credential guard
in `main.py`: if `*` is in `API_CORS_ORIGINS`, `allow_credentials` is forced off.

**Operator UI (one form for combined audits).** The standalone Social Audit page
(`pages/social.tsx`) and its nav tab were **removed**; the top nav is now just **"Website Audit"**
and **"Audit History"**. Everything runs from the Website Audit page (`pages/index.tsx`), which now
has optional Instagram / Facebook / YouTube fields — providing **any** handle makes the submission
a `combined` audit (otherwise it stays a plain `website` audit). The detail page
(`pages/audit/[id].tsx`) appends a **Social Media Audit** block and an **Overall Lead-Gen
Readiness** block at the very end for combined audits; the history list (`pages/audits.tsx`) shows
a **"Full"** badge and an Overall-score cell for combined rows; `lib/api.ts` gained the
`"combined"` audit type, `overall_score`, an `OverallReadiness` type, and
`ReportPayload.social_audit` / `overall_readiness`. **Note:** a social-*only* audit (no website
URL) can no longer be created from the UI, but the backend `audit_type="social"` path still exists
and past social audits still render in history/detail.

**Public build** (`NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true`, baked at build time): `_app.tsx`
renders without `ClerkProvider`, `middleware.ts` passes every request through, and
`lib/auth.ts::useApiToken` sends no token. The nav shows only "Website Audit" (the history page
still exists by URL for local operators; on a public deployment its API answers
403); the user menu, the rerun/refresh/share buttons, the internal QA tiles, the white-label
panel and the Search Console widget are hidden; the detail page's back link returns to the
submit form. In a teaser report the detail page shows `CtaBlock` after the executive summary
and in place of the roadmap, and hides the Search Console blocks when `show_search_console`
is false.

**White-label logo SSRF vetting.** A remote `logo_url` brand override is SSRF-vetted
(`report_branding._remote_logo_url_allowed`, mirroring the crawler's host checks) **before**
WeasyPrint fetches it at render time, so it can't point the server-side fetch at an internal host.
In public mode `brand_overrides` is ignored entirely, so visitors cannot white-label a report.

---

## 8. Tests & verification

Unit tests in `tests/unit/` (~521 tests) run on every commit (pre-commit + CI), and the QA
harness passes 11/11. `tests/integration/` exists but is empty (`.gitkeep` only).
`tests/conftest.py` makes the suite hermetic: it switches off `.env` loading for every
`Settings()` in the session and pins the credentials and paid / external-side-effect toggles,
so a developer's real keys can never turn a test run into paid OpenAI/Apify/Places calls, a
Semrush login, or a Sentry report. Unit tests therefore run with the code defaults (`full`
profile, not public, Search Console on); edition behaviour is tested with explicit settings.
Highlights:

- `test_scoring_engine.py` — rubric validation, calibration (strong ≥ / weak ≤), reproducibility.
- `test_extractors.py` — strong/weak/malformed fixtures vs expected JSON.
- `test_crawler_utils.py` — URL safety, same-site rules, HTTP-failure logic.
- `test_psi_client.py` — normalization, skip path, API-key header.
- `test_commentary.py`, `test_content_plan.py`, `test_grounding_validator.py` — deterministic content plan, schema, claim stripping.
- `test_external_seo`-family: `test_site_health.py`, `test_screaming_frog.py`, `test_google_search_console.py` — technical-crawl sweep, Screaming Frog adapter, GSC facts.
- `test_report_payload.py`, `test_pdf_renderer.py`, `test_docx_renderer.py` — report composition, pagination edges, DOCX rendering.
- `test_audit_api.py`, `test_audit_lifecycle.py`, `test_worker_collection.py`, `test_time_budget.py`, `test_qa_harness.py` — API + persistence + full worker artifacts + harness.
- Rick edition: `test_report_profile.py` (leak tests built from the source of every fix — rubric remediation, action titles, technical fixes, summary advice — across the payload, PDF, DOCX, API detail and share-link JSON), `test_public_audits.py` (visitor vs operator access, the production 403, no white-label), `test_search_console_toggle.py`, `test_social_toggle.py` (the 422, no social call or section for any job while off, old audits kept, the production pin), and `test_env_template.py` (every setting documented).
- Social + combined: the `test_social_*.py` / `test_extractor_social.py` / `test_worker_social.py` suite (extractor, scoring, worker branch, providers/registry, typed schema) plus the combined flow (`_augment_with_social`, Overall Lead-Gen Readiness, appended report sections), and `test_audit_states.py` — a tripwire that keeps the `audit_jobs.status` CHECK constraint, the model, and `JOB_STATUS_VALUES` in sync.

The hermetic QA harness (`scripts/qa_common.py`, `scripts/qa_e2e.py`,
`scripts/qa_reproducibility.py`, `make qa` / `make qa-repro`) runs the real pipeline
end-to-end on ephemeral SQLite with no PostgreSQL, Docker, or paid API keys required
(PSI / OpenAI / Screaming Frog / GSC / site-health are all forced onto their skip paths).
It is operator-run, not wired into CI. It does not pin `REPORT_PROFILE` or
`SEARCH_CONSOLE_ENABLED`, so it renders whatever `.env` sets (the teaser, in this edition); set
`REPORT_PROFILE=full` in the environment to exercise the full report.

For setup/run instructions see [`SETUP.md`](SETUP.md); to
operate the tool see [`OPERATOR_GUIDE.md`](OPERATOR_GUIDE.md).

---

## 9. Deployment

This edition deploys to **https://seo.builderleadconverter.com** on the parent's shared Linode,
as compose project `blc-rick-seo-agent`, through the manual **Deploy** workflow and
`deploy/deploy.sh` (they use the `RICK_DEPLOY_*` secrets). The parent's Caddy terminates TLS and
routes the hostname to this stack's only container on the shared `blc-edge` network: the nginx
edge proxy `rick-edge`. It strips `/api`, passes the visitor's address and https on, and
rate-limits audit starts. The api, worker, frontend and datastores stay on the project's own
network, under memory ceilings.
`docker-compose.prod.yml` pins this edition's switches (`REPORT_PROFILE=teaser`,
`PUBLIC_AUDITS_ENABLED`, `SEARCH_CONSOLE_ENABLED=false`) on both api and worker and builds the
public UI (no Clerk keys needed). It has been live since 2026-09-29; how it runs and deploys, and
its open items, are in [`DEPLOYMENT.md`](../DEPLOYMENT.md). `alembic upgrade head` runs automatically on the `api`
container start. The Dockerfiles now install **pinned** dependencies from
`requirements.txt` first, then the package itself with `--no-deps -e .`, for reproducible image
builds. (GSC OAuth tokens are stored plaintext — a documented accepted risk on the single
internal VM; see [`LIMITATIONS.md`](LIMITATIONS.md).)

---

*Last reconciled with the code: 2026-09-28 (Rick edition: report profile, public audits, Search Console switch).*
