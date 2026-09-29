"""Phase 2 social-audit data layer (standalone audit type).

- ``schema`` — the typed common fact schema (``SocialProfileFacts`` / ``SocialSummary``) that
  every normalizer builds and ``rubrics/social.yaml`` scores by ``fact_path``.
- ``providers`` — the ``SocialProvider`` adapter interface + registry (Instagram/Facebook via
  Apify, YouTube via the YouTube Data API) the collector dispatches over.
- ``apify_provider`` / ``youtube_provider`` — the low-level network backends.
- ``extractor`` — pure, deterministic normalization of raw provider payloads into ``social.*``
  facts.
- ``collector`` — orchestrates provider fetch -> extractor with graceful degradation.
- ``report`` — composes the standalone Social report payload (shared by PDF + API).
"""
