# Known limitations

An honest list of what the tool does **not** do and the behavioural caveats worth knowing before
you trust a number. What is deliberately out of scope (and why) lives in
[PRODUCT.md](PRODUCT.md); this file is about how the built thing actually behaves.

_Last reconciled: 2026-09-28 (Rick edition)._

---

## 1. Scope

Out-of-scope decisions and the open backlog now live in [PRODUCT.md](PRODUCT.md) §7–§9
(no LinkedIn, no OAuth-based social auditing, no analytics integrations, no multi-tenancy, no
object storage, no live benchmarking vendor). The caveats below are about the features that *do*
exist.

**Social data is public-scrape data.** Facebook engagement has no reach/impressions denominator
because the public page gives none — only an owner-consent Meta Graph integration would fix that,
and that integration is not built (it needs Meta App Review and Business Verification). Instagram
and YouTube carry full public post/upload data. LinkedIn is not supported at all: there is no
public company-data API, partner access is priced out of reach, and their user agreement bars
scraping.

**Connected-mode YouTube Analytics exists but is off.** When enabled it only ever reports on the
*connected* account's own channel, so it audits clients, not prospects. It is unavailable in this
edition: it rides on the Google connect flow, which `SEARCH_CONSOLE_ENABLED=false` removes.

### 1.1 Combined audit (website + social) — behavioral caveats

The combined audit runs the (untouched) website pipeline first, then the social audit, and
appends a **Social Media Audit** section + an **Overall Lead-Gen Readiness** score at the end of
one report (PDF and DOCX). Known limits of that flow:

- **A social-ONLY audit can no longer be created from the UI.** The standalone Social Audit page
  (`pages/social.tsx`) and its nav tab were **removed**; everything now runs from the Website
  Audit page (a website URL is required, social links are optional, and adding any handle makes
  it a `combined` audit). The backend `audit_type="social"` still exists and past social-only
  audits still render in history/detail — but you can't start a new social-only run without a URL
  through the UI, and in public mode (`PUBLIC_AUDITS_ENABLED`) the API refuses one too (422).
- **The combined report needs `rubrics/overall.yaml` deployed.** Overall Lead-Gen Readiness is
  config-driven (`compose_overall_readiness_score`). If `overall.yaml` is missing/unreadable or a
  provider returns bad data, the social/overall step is caught and the audit **gracefully degrades
  to a website-only report** — it never fails the whole combined job. An **explicitly combined**
  submission that degrades keeps its social section as an honest collection-failure note; an
  **auto-discovered** website audit is promoted to combined **only when the social collection
  actually produced data**, so a failed collection leaves the website report byte-identical
  (no hollow sections).
- **Auto-discovery back-fills only credentialed platforms.** A profile link discovered on the
  page is used only when its provider credential (`APIFY_API_TOKEN` for Instagram/Facebook,
  `YOUTUBE_API_KEY` for YouTube) is configured — a keyless platform is never persisted onto the
  job, so it cannot pin the social section at a permanent `partial` status. Explicit operator
  handles are kept regardless.
- **Auto-discovery's credit-line veto is deliberately conservative.** A profile link sitting in
  an attribution context ("Site by <agency>", "Photo credit …") is skipped unless its handle IS
  one of the audited domain's labels exactly (`_exact_brand_match`) — a looser substring rescue
  would let a credited photographer's `@martinez` on `martinezconstruction.com` be scored as the
  client's profile. Accepted false negative: a site whose handle doesn't contain a domain label
  (e.g. `@buildwithus` on `acmebuilders.com`) is still skipped when its icon shares a compact
  footer/parent with a third-party credit line — the operator can always type the handle
  explicitly. The design bias is to never attribute a stranger's profile to the audited business.
- **Combined social findings are deterministic (no LLM).** The appended Social Media Audit section
  is rule-derived from `social.yaml`, like the standalone social report — there is no LLM-polish
  pass in the combined flow.
- **Connected-mode YouTube (SMWA-140) is wired but dark by default.** With
  `YOUTUBE_ANALYTICS_CONNECT_ENABLED=true`, the existing Google connect consent also requests the
  YouTube Analytics scopes, and audits that include a YouTube handle attach the CONNECTED
  account's channel metrics as a "Connected YouTube analytics" block (presentation-only — nothing
  is scored). The metrics are `channel==MINE` for the connected Google account, so they are
  meaningful only when the CLIENT's account is the one connected (the owner-consent model). A
  dedicated per-platform OAuth token store (SMWA-139) is deliberately deferred — YouTube reuses
  the GSC token store, and a separate table only becomes necessary when a Meta Graph provider
  (SMWA-141, blocked on Meta App Review + Business Verification) lands. Not available in this
  edition (no Google connect flow while `SEARCH_CONSOLE_ENABLED=false`).
- **No new DB column for the headline score.** Overall Lead-Gen Readiness lives in the
  `score_breakdown` JSON (`overall_readiness`), and `audit_type` is a free `String(20)` column —
  there is **no new Alembic migration** (head is still `20260625_0005`).

---

## 2. Security & Access

- **Visitors are anonymous; operators are Clerk-gated.** With `PUBLIC_AUDITS_ENABLED` (this
  edition) the visitor endpoints — create an audit, poll it, read the report JSON, PDF and DOCX —
  are open to anyone (`require_visitor`), and the public UI build sends no token. The operator
  endpoints (history, reruns, share links, `/metrics`) keep `require_user`: Clerk when
  `CLERK_ISSUER` is set, open when it is empty — how local dev, the unit tests and the QA harness
  run — **except** that a public deployment (any `APP_ENV` other than local/dev/test) without
  `CLERK_ISSUER` answers 403, so they never fall open. Without public mode every audit endpoint is
  Clerk-gated, as in the parent app. The Google OAuth callback exists only when
  `SEARCH_CONSOLE_ENABLED` and is intentionally unauthenticated because Google calls it; it is
  protected instead by an HMAC-signed, time-limited CSRF `state`.
- **Public mode has edge rate limits, but no CAPTCHA or quota.** The edge proxy limits audit
  starts (`deploy/edge/rick-edge.conf`: per visitor a burst of 3, then 1 a minute; everyone
  together 20, then 10 a minute; 429 over that). One anonymous submission runs a Playwright crawl (up to
  `CRAWLER_MAX_PAGES`, default 10), a site-health sweep (up to 150 internal + 50 outbound URL
  checks), PageSpeed calls, and — when the site links its social profiles or the visitor adds them
  — Apify / YouTube / Google Places calls, plus a Semrush page load and a paid vision call when AI
  Visibility is on. All of it runs on one worker, one audit at a time (a real site takes ~8–10
  minutes), and on API keys shared with the parent app, so a flood both delays every visitor and
  spends the parent's quota.
- **A public report's URL is its only key.** Anyone with an `/audit/<id>` link (a random UUID)
  can read that report. Unlike a share link it never expires and cannot be revoked, and audit rows
  are never pruned (§8).
- **The teaser is a presentation filter, not a data guarantee.** Every fix is still computed and
  stored (rubric `remediation` in `score_breakdown`, action items in `commentary`) and therefore
  in database backups; only the report composition strips it.
  _Hardened (2026-06-26):_ the `azp` check now rejects a token that simply **omits** the claim
  (no longer slips past), and an optional `CLERK_ALLOWED_SUBJECTS` allowlist restricts access to
  named Clerk user IDs on top of the issuer/party checks.
- **Clerk (operator endpoints only).** This edition has no Clerk instance of its own. If one is
  added for operators, use a production instance with invitation-only sign-up — the parent's
  dev instance allows open self-registration, a known gap there.
- **SSRF protection is layered and now covers mid-render requests.** The page crawler blocks
  private/loopback hosts by default (`CRAWLER_ALLOW_PRIVATE_HOSTS=false`), validates the start
  URL, re-validates the post-redirect host, **and** attaches a request-level route guard that
  aborts any sub-resource/redirect request resolving to a private/metadata IP while a page
  renders (`CRAWLER_INTERCEPT_REQUESTS`, default true; auto-disabled when private hosts are
  allowed, e.g. the QA harness). The **site-health sweep re-validates every redirect hop**
  through the same guard, and its bot-block browser recheck is **redirect-blind by design**
  (2026-07-03) so an open redirect can't steer it. Residual caveat: submitted URLs are untrusted
  input, and in public mode anyone can submit one, so these guards are the only barrier between
  anonymous input and the server's network — keep them on (`CRAWLER_ALLOW_PRIVATE_HOSTS=false`).
- Secrets live in `.env`; there is no secrets manager integration yet.
- **White-label `logo_url` is now SSRF-vetted.** A remote logo URL supplied via brand overrides is
  validated against the same private/loopback/metadata host rules as the crawler
  (`report_branding._remote_logo_url_allowed`) **before** WeasyPrint fetches it at render time, so
  the override can't point the server-side fetch at an internal host. In public mode
  `brand_overrides` is ignored entirely, so visitors cannot white-label a report.
- Google Search Console refresh tokens are stored **plaintext** in the application database
  (only relevant when `SEARCH_CONSOLE_ENABLED`; no tokens are ever stored in this edition). For
  the single internal VM this is a **documented accepted risk** (single-tenant, internal-only DB);
  encrypting these fields at rest (or moving them into a managed secrets store) is open
  productionization work before connecting real external client accounts.

---

## 3. Crawler

- **Page cap:** up to `CRAWLER_MAX_PAGES` (default 10) total pages, homepage plus
  selected same-site internal links. Large sites are sampled, not fully crawled.
- **Same-site only.** External and cross-subdomain links are not followed.
- **Failed internal pages are recorded, not retried.** A page that times out or
  errors is logged and the audit continues on the rest.
- **JavaScript-heavy or bot-protected sites** may render incompletely or be
  blocked; results then reflect what was actually rendered.
- **Form detection errs toward credit (accepted tradeoff, 2026-07-03).** Popup/embedded
  lead forms are detected via provider signatures matched anywhere in the page HTML and a
  bounded runtime frame pass, so (a) a page merely *mentioning* a form provider (e.g. a blog
  post about Typeform) or (b) any third-party iframe containing an incidental `<form>`
  (consent manager, map/search widget) can earn `uxui.forms.present` credit. Chosen over the
  opposite failure — penalizing real popup-form sites with "no lead capture form" — which is
  what the 2026-07 report-quality review was fixing.
- **Lazy-iframe forms are timing-sensitive.** The frame pass is bounded (~3 s + scroll
  passes); an embed that loads on one run and not another can flip the form-detection facts
  between live runs. Same class as the live-site/PSI variance in §7 — the QA harness is
  unaffected (fixtures have no iframes).
- **Frame-scan precheck (accepted tradeoff, 2026-07-08).** The per-page frame pass runs only
  when the page shows an embed signal: an already-mounted child frame, an `iframe` token
  anywhere in the static HTML, or a known form-provider loader signature. A custom
  scroll-injected iframe from an *unknown* vendor whose external bundle leaves none of those
  traces is skipped — accepted to save the ~1–3 s per-page scroll nudge on iframe-less sites
  (~8–30 s per audit). Known providers stay covered by the shared signature list
  (`extractor_uxui._EMBED_PROVIDER_SIGNATURES`); add a signature there to extend both
  detection and the precheck at once.

---

## 4. PageSpeed Insights

- Requires `GOOGLE_PSI_API_KEY`. Without it, PSI rules are **skipped** (scores
  rescale around them — no penalty).
- PSI is an external service: it can rate-limit, time out, or vary between runs.
  PSI-dependent rules can therefore differ across live runs even for the same
  site. (The hermetic QA harness avoids this by skipping PSI.)

## 5. External SEO Enrichment

- The **technical crawl** slot is filled by the built-in **site health sweep** by default
  (plain-HTTP status checks over discovered internal/outbound links + sitemap.xml, plus
  duplicate/missing-metadata checks over the rendered pages). It is deterministic given the
  site's state, runs in Docker, and needs no licence. Coverage limits (`SITE_HEALTH_MAX_*`,
  time budget) are recorded as coverage notes in the report rather than silently truncated.
- The sweep's URL discovery is bounded by what the rendered pages link to plus the sitemap;
  it does not do a full-site BFS crawl, so deep orphaned sections may not be checked.
- On an **enrichment rerun**, page HTML is no longer in memory, so outbound links are not
  rechecked (noted in the report); run a fresh audit for full outbound coverage.
- **Screaming Frog is optional and deliberately NOT installed in the Docker images**: its
  CLI/headless mode is licence-gated, licences are per-individual-user (a shared server key
  for several operators violates Screaming Frog's terms and risks the key being blocked),
  and the JVM wants 2–4 GB RAM — more than the production box can spare. It remains
  supported for a licensed operator machine via `SCREAMING_FROG_ENABLED` + binary path;
  when it completes, its data fills the technical crawl slot instead of the sweep, and its
  subprocess timeout is clamped under the Celery soft time limit.
- **Search Console (only when `SEARCH_CONSOLE_ENABLED`; off in this edition).** Search Console data
  is available only for Google properties the connected account can access. No matching property means GSC and URL Inspection facts are skipped.
- The app uses official Google APIs. It does not scrape the Search Console Insights UI.
- URL Inspection is quota-limited and only runs for a small priority URL set; runs with
  per-URL failures are reported as `partial` and never count toward the score.
- Google OAuth/refresh tokens are stored **plaintext** in the `google_search_console_connections`
  table — a documented accepted risk for the single-tenant internal DB (see §2). Encrypting them
  at rest is open productionization work.
- **Sweep politeness can overshoot its deadline on rate-limiting hosts (accepted, 2026-07-03).**
  The site-health sweep honors `Retry-After` on 429s (capped at 30 s, ≤2 retries per phase), and
  those sleeps are not re-checked against the sweep's own time budget — worst case an in-flight
  lane runs ~2 minutes past the deadline before yielding. Bounded by the caps and by the Celery
  soft time limit (which marks the audit failed honestly); a persistent 429 wall also never trips
  the bot-block breaker (429 is a *response*, so it resets the consecutive-transport-failure
  counter) — such a site ends `complete`-with-few-checks or times out, rather than
  `partial: bot_blocked`.

---

## 6. Commentary

- **Website commentary is fully deterministic — there is no LLM call at all.**
  `generate_commentary()` always builds a deterministic content plan
  (`build_content_plan()`) from the extracted facts and scores and returns it with
  `status`/`provider`/`model` set to `"deterministic"`, with or without an
  `OPENAI_API_KEY`. There is no "OpenAI-then-fallback" behaviour on the website path: the
  content plan **is** the output unconditionally, so commentary is consistent run to
  run but is not LLM-written site-specific prose.
- The dormant website `_call_openai()` scaffolding and `prompts/commentary_system.md` /
  `commentary_user.md` are wired only into a **deferred polish layer** (no caller today).
  Two OpenAI paths *are* live when `OPENAI_API_KEY` is set: the **standalone social audit**
  polishes its rule-derived findings (`prompts/commentary_social_*.md`, grounded, deterministic
  fallback on any failure), and **AI Visibility** reads the Semrush dashboard with a vision model
  (§9). Neither changes a score. Under the teaser profile the social polish still runs and is
  billed, but its prose is discarded at render; public mode refuses social-only audits, so in this
  edition it runs only for social audits created with public mode off.
- The **grounding validator strips unsupported _numeric_ claims** by comparing
  numbers in the commentary against extracted facts (timeframe phrases such as
  "1–3 months" are masked first so they survive). If stripping would empty a field
  it reverts to the baseline prose. It does not catch every possible non-numeric
  inaccuracy — scores remain the deterministic source of truth, and commentary is
  explanatory only.

---

## 7. Reproducibility — the precise guarantee

- **Scores are reproducible given identical extracted facts** (verified by the
  hermetic QA harness — `make qa-repro`). The rubric engine is pure and deterministic.
- **Live sites change over time** and **PSI varies run-to-run**, so re-auditing a
  real site later can produce different facts and therefore different scores. The
  reproducibility guarantee is about the scoring engine, not about the external
  world staying still.

---

## 8. Data & Storage

- **Report storage is local filesystem only.** PDFs are written under
  `storage/reports/`; there is no object-storage backend.
- **Generated files keep the profile they were rendered with.** The worker renders the PDF and
  DOCX once, when an audit completes; the API serves them from disk but composes the JSON (and the
  UI) per request. Changing `REPORT_PROFILE` therefore switches the page immediately while older
  PDFs/DOCX keep the old profile (a pruned DOCX is regenerated with the API's current profile).
  The API and the worker must run with the same value.
- **Database migrations target PostgreSQL** (they enable `pgcrypto`). SQLite is
  only used by the QA harness via `create_all`, not via Alembic.
- **File retention is cron-driven:** `scripts/cleanup_storage.py` prunes reports,
  screenshots and tool exports older than `STORAGE_RETENTION_DAYS` (default 90), but only if
  the host cron is installed — there is no in-app scheduler. **Old audit rows are never
  pruned**, so a job older than the window keeps its DB row after its PDF is gone (the PDF
  download then 404s; the DOCX is regenerated on request). AI Visibility screenshots share one
  folder (`storage/screenshots/semrush_ai_visibility/`), which is pruned only once its newest file
  is past the window, so they accumulate while AI Visibility runs regularly.

---

## 9. Operations & Observability

- **Observability is minimal:** optional Sentry (`SENTRY_DSN`), the Clerk-gated `GET /metrics`
  JSON and the cron `scripts/health_alert.py` webhook exist; there are no dashboards and no log
  aggregation.
- **No dead-letter queue.** A crashed worker's in-flight task is redelivered
  (`task_acks_late` + `task_reject_on_worker_lost`, and the task no-ops on an already-complete
  job), but a task that raises is not retried; a job that exceeds
  `CELERY_TASK_SOFT_TIME_LIMIT_SECONDS` is marked `failed`.
- **AI Visibility (Semrush) is fragile by nature.** There is no Semrush API for it: a Playwright
  bot replays a saved Semrush session and a vision model reads a screenshot. When enabled it runs
  on **every** website/combined audit (latency + one paid vision call each). Semrush allows one
  live session per account, so a human logging into the same account evicts the bot (the full
  report then shows an honest "could not retrieve" note; the teaser simply omits the section).
  This edition shares the parent's Semrush account, so a fresh login here signs out the parent's
  bot and vice versa; selectors may drift; automating Semrush may need
  their written approval (docs/OPERATIONS.md §5). It never affects scores.
- **No horizontal-scale tuning**; a single worker processes audits one at a time, which matters
  once anonymous visitors can queue them (§2).
- The local Docker Compose stack uses a dev bind-mount and `--reload`; it is for
  development, not production serving.

---

## 10. Recommended next steps

1. **Public since 2026-09-29:** edge rate limits on `POST /audits` are in place; a CAPTCHA or daily
   quota, per-audit cost limits, and enough worker capacity for the expected traffic remain (§2).
2. If operators get a Clerk login, use a Clerk **production** instance with invitation-only
   sign-up. _(Request-level SSRF interception in the crawler is now DONE —
   `crawler_intercept_requests`; the `azp` check now rejects a missing claim and a
   `CLERK_ALLOWED_SUBJECTS` allowlist is available — see §2.)_
3. Encrypt Google OAuth/refresh tokens at rest (or move them into a secrets manager) before
   Search Console is ever turned on.
4. ~~Add data retention/cleanup for `storage/` and old audit rows.~~ **DONE** —
   `cleanup_storage` + `STORAGE_RETENTION_DAYS` (cron on the host).
5. Continue the deferred scope (live benchmarking providers and analytics) — see PRODUCT.md §9.
   _(The social audit and the benchmarking scaffold are already built.)_
