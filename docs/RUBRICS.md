# Rubric & scoring guide

How the deterministic scoring rubrics work, and how to tune them safely without
changing code. Scores come from these YAML files only — the LLM never scores.

Engine: `apps/worker/stages/scoring.py`. Rubrics: `rubrics/seo.yaml`,
`rubrics/uxui.yaml`, `rubrics/composite.yaml` (website), plus `rubrics/social.yaml`
(standalone Social Score) and `rubrics/overall.yaml` (combined-audit Overall Lead-Gen
Readiness — see §5).

_Last reconciled: 2026-07-02._

---

## 1. The Rubrics

| File | `version` | Category | Rules | Used by |
|---|---|---|---|---|
| `rubrics/seo.yaml` | `phase2-seo-v12` | `seo` | 48 | website + combined |
| `rubrics/uxui.yaml` | `phase2-uxui-v3` | `uxui` | 14 | website + combined |
| `rubrics/composite.yaml` | `phase1-composite-v1` | (weights) | — | website + combined |
| `rubrics/social.yaml` | `phase2-social-v6` | `social` | 20 | social + combined |
| `rubrics/overall.yaml` | `phase2-overall-v1` | (weights) | — | combined only |

The first three are the **website** rubrics; their combined `rubric_version` stored on a
website result is `phase2-seo-v12+phase2-uxui-v3+phase1-composite-v1`. (seo v12 is a
presentation-metadata-only bump: the finding-merge pairs moved into per-rule `merged_into`
keys; scoring is unchanged from v11.) `social.yaml` scores the
standalone **Social Score** (§5a). `overall.yaml` blends the website Lead-Gen composite with the
Social Score for a **combined** audit only (§5b). **Bump a version whenever you change a rubric**
so historical results remain interpretable.

The SEO rubric grew from the original 13 on-page rules (48 today, across the families in
§3) — first by adding two rule families that read facts collected by the worker's later
stages: **PageSpeed
Insights** (`psi.*`) and **External SEO** (`external_seo.technical_crawl.*`,
`external_seo.gsc.*`, `external_seo.url_inspection.*`). Every one of those rules is
`skip_if_missing: true`, so a site audited without PSI/GSC/Screaming-Frog credentials
is never penalized for the absent data (see §4 and §6).

---

## 2. Anatomy of a Rule

```yaml
- id: seo.meta_description.present_all_pages   # stable, unique identifier
  description: Meta descriptions are present across crawled pages.
  weight: 10                                   # relative importance (> 0)
  fact_path: seo.summary.meta_descriptions_present_pct  # where to read the fact
  evaluator: threshold                         # how to judge the value
  params:                                      # evaluator-specific config
    min: 100
    partial_min: 70
  skip_if_missing: false                       # optional; drop rule if fact absent
  # --- content-plan metadata (optional; consumed by content_plan.py) ---
  impact: high                                 # high | medium | low (default medium)
  tier: quick_win                              # quick_win | mid_term | long_term (default quick_win)
  finding_label: Meta descriptions are missing on some pages   # user-facing problem (no rule IDs)
  remediation: Add a unique 70-160 character meta description to every page.
  surface_as_finding: true                     # default true; false hides meta/health rules
```

- **`fact_path`** is a dotted path into the fact bundle
  `{seo, uxui, psi, external_seo}`, with list indexing supported
  (e.g. `seo.pages[0].title.is_reasonable_length`). A missing path scores `fail` —
  unless `skip_if_missing: true`, in which case the rule is **skipped** and excluded
  from the denominator.
- **`weight`** is relative within a category; categories are normalized to
  `max_score` (default 100).
- **Content-plan metadata** (`impact`, `tier`, `finding_label`, `remediation`,
  `surface_as_finding`) does **not** affect the score. It is read by
  `content_plan.build_content_plan` to author the deterministic findings and
  recommendations in the report (see §8). Under the **teaser** report profile (this edition's
  `REPORT_PROFILE=teaser`) `remediation`, `tier` and everything derived from them are still
  computed and stored, then removed when the report is composed
  (`report_profile.apply_report_profile`), so editing them has no visible effect there.
  `finding_label` (the finding title) and `description` (the rule lists) *are* shown in the
  teaser, so they must state the problem and never the fix.
  All five fields are optional in the schema (`RubricRule` in `scoring.py`) — the
  defaults are `impact=medium`, `tier=quick_win`, `finding_label=None`,
  `remediation=None`, `surface_as_finding=true`. Set `surface_as_finding: false`
  for non-actionable meta/health rules (e.g. "facts were extracted successfully")
  so they score but don't appear as a user-facing finding.
- **Two more presentation-only fields:** `unit` (display unit for the social report's metric
  lines, e.g. `"%"`, `" days"`) and `merged_into` (when both this rule and the named rule
  surface as findings, this one folds into that primary as a covered-by note). `merged_into` is
  **validated at load time** — an unknown target, a self-merge, or a chain (target that itself
  merges) makes the rubric fail to load, because a bad target would silently drop a finding.

---

## 3. Evaluators

| Evaluator | Passes when | Partial (ratio 0.5) | Params |
|---|---|---|---|
| `boolean` | value is `True` | — | — |
| `presence` | value is non-empty (str/list/dict/number) | — | — |
| `exact_match` | value == `full_credit` | value in `partial_credit` | `full_credit`, `partial_credit` |
| `range` | value within `full_credit` `[lo, hi]` | within any `partial_credit` range | `full_credit`, `partial_credit` |
| `threshold` | meets `min`/`max` bounds | meets `partial_min`/`partial_max` | `min`, `max`, `partial_min`, `partial_max` |
| `linear_scale` | value ≥ end of `input_range` | proportionally between start/end | `input_range: [start, end]` |

Each rule yields a result of `pass` (ratio 1.0), `partial` (0.5; proportional for
`linear_scale`), `fail` (0.0), or `skipped`. Points awarded = `weight × ratio`.

**`threshold` is overloaded by direction:**

- **Higher-is-better** — supply `min` (and optionally `partial_min`): the value
  passes when `value >= min`, partial when `value >= partial_min`. Used for
  coverage percentages (e.g. `seo.summary.image_alt_coverage_pct`).
- **Lower-is-better** — supply `max` (and optionally `partial_max`): the value
  passes when `value <= max`, partial when `value <= partial_max`. This is how
  **every external-crawl and GSC count rule** is scored — e.g.
  `external_seo.technical_crawl.summary.missing_titles` with `max: 0` /
  `partial_max: 5` (zero missing titles = pass, a few = partial, many = fail).

### Rule families and their fact sources

| Family | `fact_path` prefix | Evaluator | `skip_if_missing` | Source stage |
|---|---|---|---|---|
| On-page SEO | `seo.*` | mixed (`boolean`, `presence`, `threshold`, …) | mostly `false` | `extractor_seo.py` |
| Answer-engine readiness (AEO) | `seo.summary.all_pages_heading_hierarchy_ok`, `…total_question_headings`, `…has_extractable_structure` | `boolean`, `threshold` | `false` | `extractor_seo.py` (`_extract_aeo`) |
| Local-SEO | `seo.summary.has_complete_nap_schema`, `…has_service_area_markup`, `…has_map_or_gbp_link`, `…has_visible_address` | `boolean` | `false` | `extractor_seo.py` (`_extract_local`) |
| Accessibility (a11y) | `seo.summary.all_pages_have_lang`, `…all_pages_have_main_landmark`, `…viewport_allows_zoom`, `…total_positive_tabindex`, `…unlabeled_form_controls`, `…empty_links`, `…empty_buttons`, `…duplicate_referenced_ids` | `boolean`, `threshold` | mixed (element-dependent rules `true`) | `extractor_seo.py` (`_extract_a11y`) |
| UX/UI | `uxui.*` | mixed | mostly `false` (the form-capture, contact-path and homepage field-count rules are `true`, so pre-v3 stored facts / uncountable embeds rescale) | `extractor_uxui.py` |
| PageSpeed | `psi.summary.avg_*_performance` | `linear_scale` | **`true`** | `psi_client.py` |
| Core Web Vitals (CrUX field data) | `psi.summary.crux.*` (`lcp_p75_ms`, `inp_p75_ms`, `cls_p75`) | `threshold` (lower-is-better) | **`true`** | `psi_client.py` |
| Technical crawl | `external_seo.technical_crawl.summary.*` | `threshold` (lower-is-better) | **`true`** | `external_seo.py` / `site_health.py` |
| Search Console | `external_seo.gsc.summary.*` | `threshold` (lower-is-better) | **`true`** | `google_search_console.py` |
| URL Inspection | `external_seo.url_inspection.summary.*` | `threshold` (lower-is-better) | **`true`** | `google_search_console.py` |

> **Scope of the static accessibility module.** The `seo.a11y.*` rules are a
> *static-HTML accessibility screen*: every check is computed deterministically from the
> stored, server-rendered markup with an HTML parser — no browser, no JavaScript, no computed
> CSS, no extra fetch (so **axe-core is deliberately not used**; it needs a live rendered DOM,
> which would break the deterministic-from-stored-facts invariant). It covers only the
> low-false-positive, structural checks that are also the highest-prevalence real failures
> (WebAIM Million): language declaration, zoom permission, a main landmark, programmatic labels
> for forms / links / buttons, the positive-tabindex anti-pattern, and duplicated *referenced*
> IDs. It deliberately does **not** evaluate anything render-dependent — colour/text contrast,
> computed ARIA state, whether a labelled-by target is actually visible, keyboard focus order
> and visibility, reflow/zoom behaviour, touch-target size, or JS/CSS-injected content. The
> element-dependent count rules (`form_controls_labeled`, `links_have_name`, `buttons_have_name`,
> `unique_referenced_ids`, `viewport_zoom`) are `skip_if_missing`, so a page with no
> forms/buttons/links/id-references/viewport-meta rescales rather than being vacuously credited.
> Automated tooling of any kind reliably detects only roughly **a third to a half** of WCAG
> success criteria; absence of detected issues here is **not** a proof of conformance.

The PSI, Core Web Vitals, technical-crawl, GSC, and URL-Inspection families are **all**
`skip_if_missing: true`. When their source degrades (no API key, source returned a
non-`complete` status, or no data), the facts are absent and the rules are skipped
rather than failed — so a missing or failed source never drags the score down (§4,
and the graceful-degradation rule in §5 of [ARCHITECTURE.md](ARCHITECTURE.md)).

---

## 4. How a Category Score Is Computed

`score_category` (in `scoring.py`):

1. Evaluate every rule → `pass`/`partial`/`fail`/`skipped`.
2. Drop `skipped` rules from both numerator and denominator.
3. With `normalization: rescale_to_max` (the default):
   `score = round( (awarded_points / evaluated_weight) × max_score )`.

   > Every `round(…)` in this guide means the engine's **half-up** rounding —
   > `scoring.round_score`: `int(x + 0.5)`, clamped to `[0, max_score]` — **not** Python's
   > built-in `round()`, which rounds halves to even (`round(72.5) == 72`, the engine gives 73).
4. Clamp to `[0, max_score]`.

So skipping a rule (e.g. PageSpeed rules when PSI data is missing) **does not
penalize** the site — the remaining rules are rescaled to fill the category. A
`skip_if_missing: true` rule whose fact is absent drops out of **both** numerator
and denominator, so the category rescales around it.

This is reinforced for the External SEO families: before scoring, the engine
trusts only external sources whose `status == "complete"` — any source reporting
`partial`/`failed`/`skipped`/`empty` has its `summary` stripped
(`scoring._trusted_external_seo_facts`). The dependent rules then see a missing
fact and skip. Net effect: a degraded or unauthenticated GSC/Screaming-Frog/PSI
source neither aborts the audit nor lowers the score.

Each category breakdown records, per rule: `result`, `points_awarded`,
`points_possible`, the resolved `evidence.value`, and a `reason`. This is the
per-rule audit trail surfaced in the report and the UI.

---

## 5. Lead Generation Readiness (composite)

`rubrics/composite.yaml` combines the two website category scores:

```yaml
version: "phase1-composite-v1"
max_score: 100
weights:
  seo: 0.45
  uxui: 0.55
```

`lead_gen = round(seo_score × 0.45 + uxui_score × 0.55)`. Weights must include
exactly `seo` and `uxui` and **sum to 1.0** (validated on load). This website composite
is **untouched** by the social/combined work below — it is reused verbatim as one input
to the Overall Readiness score (§5b).

### 5a. Social Score (`rubrics/social.yaml`)

The standalone **Social audit** is scored by the same rubric engine against
`rubrics/social.yaml` (`version: phase2-social-v6`, `category: social`, 20 rules,
`normalization: rescale_to_max`, `max_score: 100`). v2 added four content-depth rules
(business/creator account, video share, posting consistency, hashtag usage); v3 is a
fact-semantics calibration in the extractor with no rule changes — video share aggregates
over non-YouTube profiles only (a channel is definitionally 100% video), Facebook post
typing no longer counts a generic reach field as video, hashtag counting requires at least
one letter ("#1" is not a hashtag), and the Instagram business-account flag is tri-state
(missing ⇒ unknown ⇒ the rule rescales instead of failing); v4 added the SAE expert-review
profile-quality/consistency/NAP/Google-reviews rules (20 rules total); v5 renormalized
posts-per-month onto the shared 30.44-day month; v6 declares a display `unit` on the numeric
rules ("%", " days", "/month" — presentation metadata consumed by the report's quantified
metric lines; scores unchanged). Facts come from
`apps/worker/stages/social/extractor.py` and use `social.*` `fact_paths` (e.g.
`social.status`, `social.summary.avg_posts_per_month`). It is scored by
`scoring.score_social_audit()` into a **standalone Social Score (0–100)** and is **not** folded
into the website composite. The `social` category was added to `Rubric.category`
(`Literal["seo", "uxui", "social"]`) so the engine loads this rubric — see §7.

### 5b. Overall Lead-Gen Readiness (`rubrics/overall.yaml`) — combined audits only

A **combined** audit (one form: a website URL **plus** ≥1 social handle) runs the untouched
website pipeline first, then the social audit, and appends an **Overall Lead-Gen Readiness**
score to the end of the single report. That score blends the two pre-computed numbers via
`rubrics/overall.yaml`:

```yaml
version: phase2-overall-v1
max_score: 100
website_weight: 0.70
social_weight: 0.30
```

`overall = round(website_lead_gen × 0.70 + social_score × 0.30)`, computed by
`scoring.compose_overall_readiness_score()` (validated by the `OverallRubric` Pydantic
model: `website_weight + social_weight` must **sum to 1.0**). Both inputs are already-computed
scores — the website Lead-Gen composite (§5) and the Social Score (§5a) — so this rubric only
weights, it never re-evaluates rules. Half-up rounding, like the rest of the engine.

**Weighting rationale:** the website is the bottom-of-funnel lead-capture surface (forms, calls,
high-intent search traffic convert there) so it carries the majority weight; social media is
top-of-funnel demand generation and nurture — meaningful but secondary.

**Rescale when social is missing:** if the social audit produced no score, the readiness
**rescales to the website Lead-Gen score alone** (`status: website_only`, the social weight
drops out) — so a combined audit whose social step degraded still gets a sensible headline
number from the website alone (and when the website Lead-Gen input itself is missing,
`website_lead_gen=None` → `status: skipped`, `score: None`). The result is stored in `score_breakdown["overall_readiness"]`
(JSON) — there is **no** new DB column.

Config knob: `RUBRIC_OVERALL_PATH` (`Settings.rubric_overall_path`, default
`./rubrics/overall.yaml`), documented in `.env.template`.

---

## 6. Tuning Workflow

1. Edit weights/params (or add/remove rules) in the relevant YAML.
2. Bump the rubric `version`.
3. Validate + re-score against the sample sites:

   ```bash
   make test          # rubric schema + scoring-engine tests
   make qa            # strong site end-to-end
   make qa fixture=weak_site.html   # weak site, to confirm calibration direction
   make qa-repro      # confirm reproducibility still holds
   ```

4. Confirm the calibration gate: the strong sample site scores meaningfully
   higher than the weak one for explainable, rule-level reasons. The committed
   gate is `test_scoring_calibrates_strong_and_weak_fixture_sites`
   (`tests/unit/test_scoring_engine.py`), which scores both fixtures **with PSI**
   (`_psi(92, 96)` strong, `_psi(35, 50)` weak) and asserts these **bounds**:

   | Site | SEO | UX/UI | Lead Gen |
   |---|---|---|---|
   | strong | ≥ 85 | ≥ 85 | ≥ 85 |
   | weak | ≤ 35 | ≤ 30 | ≤ 35 |

   > **Illustrative, pre-external-SEO snapshot.** An earlier edition of this guide
   > recorded exact scores of strong 100/100/100 and weak 21/4/12 scored *without*
   > PSI and *before* the External SEO rule family existed. Those numbers are kept
   > only as a directional illustration — they are **not** the current scores. With
   > PSI included and the external-SEO rules skipped (no GSC/crawl creds in QA), the
   > live numbers differ; trust the asserted bounds above, not the old snapshot.

---

## 7. Validation Rules (schema)

Rubrics are validated by Pydantic models on load (`Rubric`, `CompositeRubric`,
`OverallRubric`):

- Unknown keys are rejected (`extra="forbid"`).
- `weight > 0`, `max_score > 0`.
- `category` must be `seo`, `uxui`, or `social`; `evaluator` must be one of the six above.
- `impact` ∈ `{high, medium, low}`; `tier` ∈ `{quick_win, mid_term, long_term}`
  (both default-valued, so omitting them still validates).
- `normalization` is `rescale_to_max` or `sum_of_weights`.
- Composite weights must be exactly `{seo, uxui}` and sum to 1.0.
- Overall weights (`website_weight`, `social_weight`) must each be in `[0, 1]` and sum to 1.0.

A malformed rubric fails fast at load time rather than producing a wrong score.

> **`social` is now a real category, scored standalone.** `Rubric.category` is the typed
> `Literal["seo", "uxui", "social"]` in `scoring.py`, so `rubrics/social.yaml` loads and scores
> into its own Social Score (§5a) — it is **deliberately not** folded into the website composite,
> whose `weights` dict stays the typed `Literal["seo", "uxui"]`. The combined audit instead
> blends the website composite and the Social Score one level up, via `OverallRubric` /
> `rubrics/overall.yaml` (§5b). Adding a *further* website composite category would still require
> a typed code change (widen the composite `Literal`, extend the composite validation); adding a
> new social backend does not touch the rubrics.

---

## 8. Rule metadata drives the findings (why commentary is deterministic)

*(`commentary.py`, `content_plan.py`, `rubrics/seo.yaml` and `rubrics/uxui.yaml` point here.)*

**The report's structure is data, not prose.** `content_plan.build_content_plan()` is the single
source of truth for what a report says — which findings exist, their order, severity, remediation
tier, and baseline wording. It is a pure function of the score breakdown plus the extracted facts,
so the same site yields the same findings on every run, with or without an OpenAI key.

This exists because the original design had **two divergent prose generators**: with an API key the
LLM authored everything (which findings existed, how many, how severe); without one, a thinner code
path built a different structure. Re-running the same site produced a structurally different
report. A point of score wobble is fine (PageSpeed varies); structural drift is not.

**The invariant: an LLM may rewrite prose, never add, drop, reorder or invent a finding.** OpenAI
being down, slow or creative can only change how polished a report reads, never what it says.
"Fallback" is not a different report — it is the same report without the polish step.

### How a rule becomes a finding

Each `RubricRule` carries presentation metadata that never affects the score:

| Field | Meaning |
|---|---|
| `impact` (`high\|medium\|low`) | how much it matters **when it fails** |
| `tier` (`quick_win\|mid_term\|long_term`) | remediation horizon, independent of the result |
| `finding_label` | the client-facing title (never the internal `rule_id`) |
| `remediation` | the action item |
| `surface_as_finding` | `false` for non-actionable meta rules (e.g. `*.collection.complete`) |
| `unit` | display unit for quantified metric lines (`"%"`, `" days"`, `"/month"`) |
| `merged_into` | fold this finding into another as a covered-by note when both surface |

- **Selection:** a rule becomes a finding only when `result` is `fail` or `partial` **and**
  `surface_as_finding` is true. `pass` and `skipped` never do — which is why a missing PSI or GSC
  key can never manufacture a finding.
- **Severity = impact × result:** high/fail → high, high/partial → medium, medium/fail → medium,
  medium/partial → low, low/fail → low, low/partial → info (`_SEVERITY_MATRIX` in `content_plan.py`).
  A merged card adopts its group's strongest severity, so an absorbed `fail` can't vanish.
- **Ordering** is stable and total (severity, then weight, then rule id), so two runs can't shuffle
  the report.
- **Recommendations are the same selected rules**, re-sorted for display —
  `COMMENTARY_MAX_FINDINGS_PER_SECTION` is therefore the *only* truncation knob. (An earlier
  tier-first recommendation sort could push a long-term fix past the cap, printing a problem with
  no fix.)
- **Teaser profile:** the plan above is always built and stored in full. With
  `REPORT_PROFILE=teaser` the composed report drops every action item, recommendation, tier and
  roadmap entry, rewords the "Start by checking" location labels, and removes the executive
  summary's closing advice; findings (title, severity, what it means, why it matters, where it
  was found) are unchanged. See `apps/worker/stages/report_profile.py`.

### Rules that must not change

1. **Grounding-safety by construction.** Baseline prose emits only a rule's resolved fact value or a
   section/composite score — both present in the grounding validator's fact sources, so the
   deterministic baseline is never stripped. A new template needing a derived aggregate must add
   that aggregate to the extractor summary first.
2. **Evidence cites `fact_path`, never `rule_id`** — fact paths are meaningful to a reader; rule ids
   are internal and once leaked into client-facing titles.
3. **`extra="forbid"` ordering trap.** `RubricRule` rejects unknown keys, so a new metadata field
   must land in the Pydantic model in the *same change* as the YAML that uses it, or every rubric
   fails to load.
4. **Grounding never widens its trusted set.** Rule weights and ratios are scoring mechanics, not
   citable numbers. `UNGROUNDED_KEYS` exempts `evidence_refs`, `action_items`, `location_urls` and
   `location_label` *only* because those are never LLM-written (prescriptive advice legitimately
   carries target numbers like "70–160 characters"). If a polish layer ever touches them, that
   exemption must be revoked.
5. **A fully-stripped field reverts to the deterministic baseline**, never to a placeholder string
   in a client's report.
6. **Any metadata edit bumps the rubric `version:`** — it is recorded in `audit_results.rubric_version`.

---

## 9. The GSC "ranking opportunity" forecast — how to defend the number

> **Not in this edition:** with `SEARCH_CONSOLE_ENABLED=false` (the Rick edition) Google is never
> called, so this forecast is never produced and the three `seo.gsc.*` rules (weights 6/5/6)
> always skip; they stay in `score_breakdown` but are left out of the report's rule lists.

The report's biggest claim is a traffic projection, so the model is deliberately conservative and
every input is stored as a fact. Built by `_opportunity_estimate` in `google_search_console.py`; it
is a projection, never a guarantee, and never a revenue or CAC figure (the tool has no CRM data).

1. **Who is modeled — "striking distance".** Only the client's own Search Console *queries* (never
   pages) with **≥50 impressions** at **average position 4–20**, and only the **top 25 by
   impressions**. Projecting every ranking query moving at once is not defensible.
2. **Target ranks — never #1.** Upside is modeled at **position 5 (low) and position 3 (high)**. A
   query already out-performing the curve contributes zero, never a negative.
3. **CTR curve — the conservative blend, versioned like a rubric.** `blended-conservative-v1`:
   P1 27.6% → P3 11.0% → P5 6.1% → P10 2.2%, clamped past position 10. It sits in the 20–28% P1 band
   of GSC-derived studies (Backlinko / SISTRIX / seoClarity); the First Page Sage curve (P1 39.8%) is
   the optimistic outlier and is deliberately not used. Curve source and version ship in the estimate.
4. **AI-Overview haircut — position-aware, not flat.** Upside is multiplied by
   `1 − prevalence × CTR-reduction-at-that-rank`, using Ahrefs' Dec-2025 ~300k-keyword study
   (−58.0% at rank 1 decaying to −19.4% at rank 10). Prevalence defaults to **0.40** of commercial
   SERPs (`GSC_OPPORTUNITY_AIO_PREVALENCE`). At the defaults that is **≈13% off at target position 5
   and ≈19% at position 3** — the optimistic target is discounted harder, because AI Overviews
   suppress clicks most at the top.
5. **Capture scenarios — the headline is the conservative one.** 50% / 70% / 100% of the modeled
   upside; the report leads with **50%**.
6. **Reality cap.** No scenario may exceed **3× the site's current monthly organic clicks**
   (`GSC_OPPORTUNITY_CAP_MULTIPLE`). A zero-click site gets *no* cap, since capping at zero would
   pin every scenario to a meaningless floor.
7. **Monthly normalization.** Every published figure is a true monthly rate (`÷ window_days / 30.44`),
   and the collection window ships with the estimate so tables can state their real date range.
8. **Suppressed entirely** when no query qualifies, or when the conservative monthly high bound
   rounds to 0 — so a degenerate "0 to 0 visits per month" claim can never ship.
9. **Leads are a labeled industry range, never a promise.** Headline clicks × **5–10%**, a published
   home-services contact-conversion benchmark *not measured on the audited site*, and never
   multiplied by a job value.

**Defending it in one breath:** "We took only your own Search Console queries that already rank 4–20
with real impression volume, modeled just the top 25 of them reaching positions 3–5 (never #1),
priced the gain with a conservative published CTR curve, discounted it for AI Overviews, assumed you
capture only half the upside, capped the whole thing at 3× your current monthly clicks, and stated
it per month over a named date window."
