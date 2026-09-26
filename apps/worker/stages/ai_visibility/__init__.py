"""AI Visibility enrichment (Semrush AI Visibility Toolkit).

An optional, on-demand report section that reports how a brand appears in AI answers
(ChatGPT / Google AI Overviews / AI Mode / Gemini / Perplexity …), sourced from the Semrush
AI Visibility Toolkit. Semrush exposes no API for this toolkit, so the data is collected by a
Playwright bot that signs into the operator's own Semrush account, opens the AI Visibility
overview for the audited domain, screenshots the dashboard, and extracts the numbers with an
OpenAI vision model into typed facts.

Design mirrors the ``benchmarking`` / ``social`` enrichment layers:
- typed facts (:mod:`schema`), a pure report builder (:mod:`report`), a provider registry
  (:mod:`providers`), and a graceful-skip collector (:mod:`collector`);
- **presentation only** — it NEVER feeds the deterministic scoring engine (scores are unchanged
  whether it ran or not);
- **on-demand only** — run from a dedicated Celery task/endpoint, never the always-on audit
  pipeline, so reproducibility (``make qa-repro``) is untouched and no Semrush login happens on a
  normal audit.
"""
