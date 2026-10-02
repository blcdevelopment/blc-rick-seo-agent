# User guide

How to run an audit and read the result in the Rick edition. For what the tool is, see
[PRODUCT.md](PRODUCT.md); for server-side operations, [OPERATIONS.md](OPERATIONS.md).

---

## 1. Access

**Visitors never sign in.** The public site (`PUBLIC_AUDITS_ENABLED` plus a UI built with
`NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true`) lets anyone submit a website and read its report. A report
lives at `/audit/<id>`, where the id is a random UUID: anyone who has that link can open the report
and download its PDF and DOCX, and the link does not expire.

The operator features — audit history, share links, reruns, metrics — are API endpoints guarded by
Clerk. With no Clerk configured they are open on a local machine (`APP_ENV=local`) and answer
**403** *("Operator endpoints are disabled on this public deployment.")* anywhere else. See §8.

## 2. Starting an audit

The **Website Audit** page is the only submission form:

| Field | Notes |
|---|---|
| **Website URL** | Required. The only mandatory field |
| **Niche** | Optional. Printed on the report cover |
| **Target audience** | Optional. Printed on the report cover |

**Social media is not part of this edition's audit** (`SOCIAL_AUDITS_ENABLED=false`, with the UI
built with `NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED=false`). The form has no social fields, the audit
does not look at the site's Instagram / Facebook / YouTube links, and the report has no Social
Media Audit section and no Social or Overall Lead-Gen Readiness score. A request that carries
social handles is refused with *"Social media audits are not available here."* Reports completed
before the switch keep the social section they had.

## 3. Watching progress

The audit page polls every 2.5 seconds and shows the stage and percentage:

```
queued → crawling (15%) → PageSpeed (45%) → extracting SEO + UX/UI (70%)
       → external SEO (76%) → scoring (80%) → commentary (88%) → validating (95%)
       → [if AI Visibility is on: "Collecting AI visibility insights" (97%)]
       → rendering (98%) → complete (100%)
```

A real site typically takes several minutes (PageSpeed and the link check dominate). On failure the
status becomes **failed** with a message, and the audit can simply be re-submitted.

## 4. Reading the scores

| Score | Meaning |
|---|---|
| **SEO** | Search-visibility fundamentals: titles, meta descriptions, headings, canonicals, schema, alt text, indexability, internal links, the site-wide technical crawl, local-SEO and answer-engine structure, security, Core Web Vitals |
| **UX/UI** | Conversion signals: CTAs, lead forms (including popups and embeds), contact paths, trust signals, navigation |
| **Lead Generation Readiness** | The headline number — 45% SEO + 55% UX/UI |

There is no Social or Overall Lead-Gen Readiness score in this edition (social audits are off).

Colour bands are identical in the UI and the PDF: **≥75 strong, ≥50 fair, <50 weak.**

Each score card says how many checks it evaluated and which deductions were biggest; the full
per-rule breakdown is stored with the audit (and returned by the API) rather than printed. A
**skipped** check — PageSpeed with no API key, an external source that was unavailable — does **not**
lower the score; the category rescales around it. Scores are deterministic: the same facts always
produce the same numbers.

## 5. What the report shows (the teaser)

This edition renders the **teaser** profile (`REPORT_PROFILE=teaser`):

- **Shown:** the scores, an executive summary, and every finding with its severity, what it means,
  why it matters and where it was found; the site-health issues and PageSpeed metrics.
- **Not shown:** anything that says how to fix a problem — no "Do this" steps, no recommendations,
  no roadmap or timeframes, no technical "recommended fix", no accessibility fix guidance.
- **Instead:** a **"Get the fixes"** call-to-action after the executive summary and again where the
  roadmap would be (a **Next Steps** page in the PDF), with the `BOOKING_CTA_LABEL` text ("Book a
  meeting with Rick") linking to `BOOKING_URL`. With no URL configured it shows the label only.

The web page, the PDF and the DOCX always agree. A PDF or DOCX is rendered once, when the audit
completes, so a later change to the call-to-action or profile shows up in new reports only.

## 6. Downloading

- **PDF** is the primary deliverable; the **DOCX** is rendered alongside it (and regenerated on
  request if the file is missing).
- To pass a report on, share its `/audit/<id>` page link.

## 7. AI Visibility (how the brand shows up in AI answers)

When enabled on the server, this section is read from the Semrush AI Visibility Toolkit and runs
automatically on every website and combined audit. It is **presentation only: it never changes a
score.** It needs a saved Semrush session; when there is none (or it has expired) the teaser simply
leaves the section out ([OPERATIONS.md](OPERATIONS.md) §5).

## 8. For operators

- **Search Console is switched off in this edition** (`SEARCH_CONSOLE_ENABLED=false`): no Google
  connection, no Search Console sections, and its checks are skipped.
- **Social media audits are switched off in this edition** (`SOCIAL_AUDITS_ENABLED=false`): no
  social discovery, no Apify / YouTube / Google Places calls, no social section. A combined audit
  queued before the switch runs as a website audit; a social-only one fails with the message in §2.
- **Audit history** is not in the public navigation. Locally (no Clerk, `APP_ENV=local`) it is at
  `/audits`: every audit with a type badge (**Web**, **Full**, **Social**) and its scores; search,
  filter and sort work client-side over the rows loaded (up to 100).
- **Share links, Rerun enrichment, Refresh AI Visibility and white-label branding** are hidden in the
  public build. They remain API endpoints (`POST /audits/{id}/share`,
  `POST /audits/{id}/rerun-enrichment`, `POST /audits/{id}/rerun-ai-visibility`) for an operator with
  Clerk access or on a local machine; in public mode the API ignores white-label branding.

## 9. When something looks off

| Symptom | Likely cause | What to do |
|---|---|---|
| Report has no Social section | Expected: social audits are off in this edition | Nothing to do |
| An older report still has a Social section | It was completed before social audits were switched off | Nothing to do |
| The form still shows social media fields | The UI was built without `NEXT_PUBLIC_SOCIAL_AUDITS_ENABLED=false` | Rebuild the frontend |
| PageSpeed numbers missing | No PageSpeed API key configured | Expected — those rules skip and don't lower the score |
| No AI Visibility section | AI Visibility is off, or there is no valid Semrush session | See [OPERATIONS.md](OPERATIONS.md) §5 |
| The site asks visitors to sign in | The UI was built without `NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED=true` | Rebuild the frontend |
| PDF download 404s | The file was pruned by retention, or the render failed | Re-run the audit |
| Audit stuck in one stage for many minutes | A slow site, or the worker died | Wait for the time limit to fail it honestly, then re-submit |
