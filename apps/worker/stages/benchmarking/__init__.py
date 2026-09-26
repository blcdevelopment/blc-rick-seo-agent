"""Competitor benchmarking data layer (P2-26 / SMWA-79 — Epic P2-E5 Enrichment, v3).

DEFERRED SCAFFOLD. This is the graceful-skip seam for presenting the audited scores relative to
competitor / industry baselines. It mirrors the social-audit layout (schema + providers + registry
+ collector) and the missing-key skip pattern shared with PSI / Apify / GSC. It is a **no-op by
default** and, because there is no live paid-vendor client yet, it stays a no-op even when enabled
until a vendor is selected and its recurring cost is approved (the ticket's acceptance gate). Until
then the report is byte-identical and no cost is incurred.

- ``schema`` — the typed benchmark fact models (``CompetitorBaseline`` / ``BenchmarkFacts``);
  ``extra="forbid"`` so a drifted field is a hard error.
- ``providers`` — the ``BenchmarkProvider`` adapter interface + registry (SEMrush / Ahrefs /
  Similarweb stubs) the collector dispatches over; the live HTTP client is the deferred paid part.
- ``collector`` — orchestrates provider fetch -> normalize with graceful degradation (disabled /
  no provider / missing key / not-yet-implemented all => ``skipped``).
- ``report`` — pure, deterministic builder turning benchmark facts + the audited scores into the
  report's Competitor Benchmarking section (score-vs-baseline deltas).
"""
