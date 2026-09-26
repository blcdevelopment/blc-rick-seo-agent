# Operator guide

How to run an audit and read the result. For what the tool is, see [PRODUCT.md](PRODUCT.md); for
server-side operations, [OPERATIONS.md](OPERATIONS.md).

---

## 1. Signing in

The app lives at **https://ai.builderleadconverter.com** and is gated by Clerk — you need an
invited account. Two failure modes worth telling apart:

- **401** — your session token is missing, invalid or expired. Sign in again.
- **403** *("This account is not authorized to use the audit tool")* — you are signed in, but your
  account isn't on the server's operator allowlist. Ask an admin to add you.

## 2. Starting an audit

The **Website Audit** page is the only submission form. Fields:

| Field | Notes |
|---|---|
| **Website URL** | Required. The only mandatory field |
| **Niche** | Optional. Recorded on the report, and used to judge whether the social profile's business category matches the niche |
| **Target audience** | Optional. Recorded on the report |
| **Social media** *(collapsible)* | Optional Instagram / Facebook / YouTube links or `@handles`. **Leave blank and the site's own profile links are auto-detected** from its header/footer |
| **White-label branding** *(collapsible)* | Optional client name, short name, product name, primary/accent hex colours and a logo URL — applied to **the PDF only** |

Providing (or auto-detecting) any social profile turns the run into a **combined audit**: the
website audit runs first, then the social audit, producing one report with a Social Media Audit
section and an Overall Lead-Gen Readiness score at the end.

If the social data can't be collected — no provider key, a private profile, a failed scrape — the
audit still completes as a **website-only report**. That is the designed behaviour, not an error.

## 3. Watching progress

The audit page polls every 2.5 seconds and shows the stage and percentage:

```
queued → crawling (15%) → PageSpeed (45%) → extracting SEO + UX/UI (70%)
       → external SEO (76%) → scoring (80%) → commentary (88%) → validating (95%)
       → [combined only: "Auditing social profiles" (96%)]
       → [if AI Visibility is on: "Collecting AI visibility insights" (97%)]
       → rendering (98%) → complete (100%)
```

Most audits take from well under a minute to a few minutes. AI Visibility adds noticeable time — it
is a live Semrush page load plus an image read. On failure the status becomes **failed** with a
message, and the audit can simply be re-submitted.

## 4. Reading the scores

| Score | Meaning |
|---|---|
| **SEO** | Search-visibility fundamentals: titles, meta descriptions, headings, canonicals, schema, alt text, indexability, internal links, the site-wide technical crawl, local-SEO and answer-engine structure, security, Core Web Vitals, and Search Console facts when connected |
| **UX/UI** | Conversion signals: CTAs, lead forms (including popups and embeds), contact paths, trust signals, navigation |
| **Lead Generation Readiness** | The headline website number — 45% SEO + 55% UX/UI |
| **Social** *(combined/social only)* | 0–100 across the audited Instagram / Facebook / YouTube profiles |
| **Overall Lead-Gen Readiness** *(combined only)* | 70% website Lead-Gen + 30% Social. The website dominates because it is the bottom-of-funnel capture surface while social is top-of-funnel demand generation. With no social score it rescales to the website score alone |

Colour bands are identical in the UI and the PDF: **≥75 strong, ≥50 fair, <50 weak.**

Every score has a per-rule breakdown (pass / partial / fail / skipped) with its evidence. A
**skipped** rule — PageSpeed with no API key, an external source that was unavailable — does **not**
lower the score; the category rescales around it. Scores are deterministic: the same facts always
produce the same numbers.

## 5. Downloading and sharing

- **PDF** is the primary deliverable; **DOCX** is rendered on demand the first time you ask for it.
  A *standalone social* audit has PDF only — no DOCX.
- **Share** mints a random, time-limited link (7 days by default). Anyone with the link can view and
  download that one report without signing in — **the link is the secret**. *Copy* puts it on the
  clipboard, *Refresh link* mints a new token, *Revoke* kills it immediately. A revoked or missing
  link returns 404; an expired one returns 410.

## 6. Re-running parts of a finished audit

Both actions are only available once the audit is **complete**, and both keep the existing report if
they fail:

- **Rerun enrichment** — re-collects external SEO (technical crawl + Search Console), then rescores,
  rewrites commentary and re-renders. It does not re-crawl the site or re-run PageSpeed. On a
  combined audit it keeps the stored social section and recomputes the Overall score.
- **Refresh AI Visibility** — re-runs only the Semrush AI-visibility read and re-renders.

## 7. AI Visibility (how the brand shows up in AI answers)

When the server has it enabled, this section is read from the Semrush AI Visibility Toolkit and
**runs automatically on every website and combined audit** — you don't request it. It is
**presentation only: it never changes a score.**

If the section says the connection is unavailable, the saved Semrush session has expired and an
operator has to re-mint it once (see [OPERATIONS.md](OPERATIONS.md)). Semrush allows **one live
session per account**, so a human signing into the same Semrush login evicts the bot. If the feature
is switched off server-side, the refresh button returns a clear "AI visibility is disabled" error and
nothing is queued.

## 8. Connecting Google Search Console

The Search Console panel appears on both the submission and history pages. Connect once with a
Google account that has verified access to the property; afterwards new audits (and
**Rerun enrichment** on old ones) include real search-query data — impressions, clicks, average
position, and the ranking-opportunity forecast. Without it, those sections simply report no data.

## 9. Audit history

The **Audit History** page lists recent audits with a type badge — **Web**, **Full** (combined) or
**Social** — plus the Overall, Lead, SEO, UX and Social scores. Search, status filter and sort work
**client-side over the rows already loaded** (up to 100), so a much older audit may simply not be in
the loaded window.

## 10. When something looks off

| Symptom | Likely cause | What to do |
|---|---|---|
| Report has no Social section | No social profile given or auto-detected, or collection failed/keyless | Re-submit with explicit profile links; if it persists, the server may be missing the provider key |
| Social section says data couldn't be collected | Private/renamed profile, or the provider returned nothing | Verify the handle resolves publicly in a browser |
| PageSpeed numbers missing | No PageSpeed API key configured | Expected — those rules skip and don't lower the score |
| Search Console section empty | Not connected, or the account lacks access to that property | Re-connect with an account verified for the property |
| AI Visibility says unavailable | Saved Semrush session expired or evicted | Ask an operator to re-mint it (OPERATIONS.md) |
| PDF download 404s | The file was pruned by retention, or the render failed | Re-run the audit |
| Audit stuck in one stage for many minutes | A slow site, or the worker died | Wait for the time limit to fail it honestly, then re-submit |
