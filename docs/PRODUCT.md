# Product & scope

What the tool is, what it produces, what is deliberately not built, and what is still open.
For how it works internally see [ARCHITECTURE.md](ARCHITECTURE.md); for how to drive it see
[OPERATOR_GUIDE.md](OPERATOR_GUIDE.md).

---

## 1. What this is

An internal Builder Lead Converter tool that turns a prospect's web presence into a branded,
client-ready audit report. An operator pastes a website URL (optionally with Instagram / Facebook /
YouTube links); the system crawls the site, measures it, scores it against versioned YAML rubrics,
and renders a PDF (plus DOCX) the sales team can send.

- **Audience of the report:** home builders, remodelers and local service businesses — the wording
  in `prompts/` and the rubric remediation text assumes that reader.
- **Users of the tool:** ~5 internal operators, signed in through Clerk at
  `https://ai.builderleadconverter.com`.
- **Core promise:** *the numbers are defensible*. Scores come from deterministic rules over
  extracted facts, never from a language model. Identical facts always produce identical scores.

## 2. The three audit types

| Type | Input | Produces | Status |
|---|---|---|---|
| `website` | URL | SEO, UX/UI and Lead-Gen Readiness scores + PDF/DOCX | The default path |
| `combined` | URL **and** ≥1 social link | The website report with a **Social Media Audit** section and an **Overall Lead-Gen Readiness** score appended | The headline flow |
| `social` | ≥1 social link (no URL) | A standalone Social Score + its own PDF (no DOCX) | Backend-only — the UI page was removed 2026-06-26; still reachable via `POST /audits` |

A job is exactly one type, but a `website` job **can be promoted to `combined` mid-run**: if the
crawled site links to its own social profiles and a provider credential is configured, the social
step runs, and promotion happens only when the collection actually returns usable data
(`SOCIAL_AUTODISCOVERY_ENABLED`, default on).

**Scoring shape:** Lead-Gen Readiness = 0.45 × SEO + 0.55 × UX/UI (`rubrics/composite.yaml`).
Overall Readiness = 0.70 × Lead-Gen + 0.30 × Social (`rubrics/overall.yaml`), and rescales to the
website score alone when social produced nothing. Bands: ≥75 strong, ≥50 fair, <50 weak.

## 3. What each area evaluates

| Area | Focus | Evaluates |
|---|---|---|
| **SEO** | Organic visibility | Meta titles/descriptions, heading structure, internal linking, schema, indexability, image alt coverage, the site-wide technical crawl, local-SEO signals, answer-engine structure, security, Core Web Vitals, Search Console facts when connected |
| **UX/UI** | Conversion & lead capture | Value-proposition clarity, CTA visibility, lead forms (including popup and embedded), contact paths, trust signals, navigation, funnel friction |
| **Social** | Audience growth & nurture | Bio optimisation and CTA clarity, posting cadence and consistency, engagement rate, content mix, link-in-bio funnel integration, cross-platform handle consistency |

Every recommendation must serve one of two outcomes: **attract more qualified traffic**, or
**convert traffic into leads**. Reports carry an executive summary, findings per area, the score
breakdown with a per-rule trail, and a roadmap split into Quick Wins (0–30 days), Mid-Term
(1–3 months) and Long-Term (3–12 months).

## 4. Invariants (the product contract)

- **Hybrid scoring.** Deterministic Python rules produce every number; an LLM never produces or
  changes a score. Never invert this.
- **Grounded generation.** An LLM may only commentate on facts already extracted; numeric claims are
  validated against those facts and unsupported ones are stripped.
- **Config-driven rubrics.** Scoring lives in versioned YAML, not code — a weight change is a config
  change, and the version is recorded on every result.
- **Structured pipeline, not autonomous agents.** Extract → Score → Commentate → Validate, fixed.
- **Graceful degradation.** A missing or failed external source (PageSpeed, Search Console, the
  technical crawl, a social provider) never penalizes a score and never aborts an audit.

Quality bars: **reproducibility** (same facts → same score, every time), **explainability** (every
score has a per-rule breakdown), **grounding** (every numeric claim traces to an extracted fact),
and **polish** (the PDF is presentable to a prospect, not a dev artifact).

## 5. What one run produces

- Rows in `audit_jobs` + `audit_results` (facts, per-rule score breakdown, commentary, validation log).
- `storage/reports/<job_id>.pdf` and `.docx`, plus page screenshots.
- Optionally a time-limited public share link (`/shared/<token>`) so a prospect can read the report
  without an account.

## 6. What is built

- **Website audit:** Playwright crawl (SSRF-guarded, robots-aware) → PageSpeed Insights → SEO and
  UX/UI fact extraction → external-SEO technical crawl (built-in site-health sweep by default;
  Screaming Frog optional; Google Search Console when connected) → 48 SEO + 14 UX/UI scored rules →
  deterministic findings → grounding check → branded PDF/DOCX.
- **Social audit:** Instagram + Facebook via Apify actors, YouTube via the free Data API v3 — public
  data only, no login. 20 scored rules.
- **Combined audit** with Google Business Profile enrichment (Places API) and a tri-way website ↔
  social ↔ GBP phone (NAP) check.
- **AI Visibility:** how the brand appears in AI answers, read from the Semrush AI Visibility
  Toolkit by a saved-session browser bot + an OpenAI vision pass. Presentation-only, never scored.
  Auto-runs on every website/combined audit when enabled — see [OPERATIONS.md](OPERATIONS.md).
- **Delivery:** Clerk-gated Next.js operator UI, share links, per-client white-label branding on the
  PDF, on-demand DOCX, external-SEO re-run, AI-visibility refresh.
- **Ops:** single Linode VM (Docker Compose behind Caddy), CI/CD auto-deploy on merge to `main`,
  optional Sentry, gated `/metrics`, cron storage retention and backups.
- **Off by default but built:** competitor-benchmarking seam (no vendor client), advisory axe-core
  accessibility pass, connected-mode YouTube Analytics.

## 7. Scope boundaries (deliberate)

- **Public data only.** Social auditing reads public profiles; it never asks a prospect to connect an
  account. Owner-consent routes (Instagram Business Discovery, Meta OAuth) were rejected because they
  only work for accounts that opt in — which defeats auditing a prospect you haven't met. LinkedIn is
  excluded outright (scraping enforcement risk).
- **One shared organisation.** No tenants, no roles, no per-user data partitioning.
- **Reports live on the box.** Local filesystem + a retention cron; object storage was evaluated and
  removed by decision.
- **Page-count ceiling by design.** A real-browser crawl costs seconds per page, so an uncapped
  full-site crawl would make cost-per-audit unpredictable. `CRAWLER_MAX_PAGES` defaults to 10 pages
  *total* (homepage included) and is capped at 50.
- **The LLM never scores.** It may rewrite prose (standalone social audits) or read a screenshot
  (AI Visibility). It can never add, drop, reorder or invent a finding.

## 8. Decided against — with the trigger that would reopen it

| Decision | Why | Reopen if |
|---|---|---|
| Don't fold Social into the website composite | `composite.yaml` stays `{seo, uxui}`; the combined audit already blends both append-only via `overall.yaml`. Folding it in would bump the composite version and **re-score every historical website audit** | Never, realistically — it breaks the reproducibility boundary |
| Apify, not Bright Data | Apify's free tier covers IG/FB at internal volume; Bright Data is paid-only with a more complex trigger→poll→download flow | Apify actors break, or volume outgrows the free tier |
| No object storage (S3) | One internal VM, ~5 users; local FS + cleanup cron is sufficient | A second node, or off-box durability becomes a requirement |
| Plaintext Google OAuth tokens in the DB | Single VM: DB access and any encryption key would belong to the same operators, so co-located encryption adds churn, not protection | Those credentials ever outlive that trust boundary. Meanwhile: keep DB dumps on-box and access-controlled |
| No Celery retry/DLQ | Deterministic failures shouldn't be retried; a crashed worker already redelivers its in-flight task | Transient failures become visibly common |
| No multi-tenancy / RBAC | Internal tool, one org | It ever ships outside BLC |

*(The full Bright Data swap map and the S3 revival sketch live in git history — `PRODUCT.md`, removed 2026-09-16.)*

## 9. Backlog — wanted, not built

| Item | Note |
|---|---|
| **Competitor benchmarking — live vendor** | The whole seam ships (registry, typed facts, graceful skip, PDF/DOCX/UI rendering); every vendor `fetch` is a deliberate no-op. Gated on an approved recurring Semrush/Ahrefs/Similarweb subscription. Implementing one `fetch` is the entire remaining task |
| **Clerk production instance** | Still a dev instance with open self-registration. Two actions: set Clerk to invitation-only, then migrate to `pk_live` on the custom domain. `CLERK_ALLOWED_SUBJECTS` is the interim mitigation |
| **YouTube Analytics connected mode** | Provider, consent wiring and report block are built and flag-gated off; flipping it on is the remaining work. Owner-consent only, so it audits clients, not prospects |
| **GA4 analytics** | No client exists. Moves the product from anonymous public-data audits to user-authorized OAuth data — a different phase. (The Search Console half of that idea *is* built.) |
| **Per-platform OAuth token store** | Only the Google/GSC token table exists, which YouTube Analytics reuses. Deliberately deferred until a Meta provider needs non-Google tokens |
| **TikTok backend** | One provider class + one registry entry away |
| **Enable the axe-core advisory pass in prod** | Built and off: it adds crawl latency and is not reproducible run-to-run, unlike every scored section |

## 10. Where the history went

This repo used to carry 19 docs, most of them plans for work that has shipped (the phase-2 epics,
report-quality and social-enhancement Jira boards, AI-visibility vendor selection). They were
deleted on 2026-09-16 and remain in git history; their durable conclusions are folded into the
seven docs that remain. `git log --diff-filter=D --name-only -- docs/` lists them.
