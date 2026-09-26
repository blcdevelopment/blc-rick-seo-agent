"""Google Places (New) normalizer + graceful-skip collector (SAE-12)."""

import json
from pathlib import Path

from pydantic import SecretStr

from apps.shared.config import Settings
from apps.worker.stages.social.places_provider import (
    collect_google_business_facts,
    normalize_google_business,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_normalizer_flattens_place_details() -> None:
    raw = json.loads((FIXTURES / "google_places_details.json").read_text())
    gbp = normalize_google_business(raw)
    assert gbp["name"] == "Acme Studio"
    assert gbp["address"] == "123 Main St, Austin, TX 78701, USA"
    assert gbp["phone"] == "(555) 100-2000"  # national preferred over international
    assert gbp["category"] == "General Contractor"  # primaryTypeDisplayName.text
    assert gbp["types"][0] == "general_contractor"
    assert gbp["rating"] == 4.7
    assert gbp["review_count"] == 128
    assert gbp["website"] == "https://acmestudio.example"
    assert gbp["business_status"] == "OPERATIONAL"


def test_normalizer_is_none_safe_on_sparse_payload() -> None:
    gbp = normalize_google_business({})
    assert gbp["name"] is None
    assert gbp["phone"] is None
    assert gbp["rating"] is None
    assert gbp["review_count"] is None
    assert gbp["types"] == []


def test_collector_skips_without_api_key() -> None:
    # _env_file=None isolates from the local .env (which now carries a real Places key).
    result = collect_google_business_facts(Settings(_env_file=None), query="Acme Studio Austin")
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_google_places_api_key"


def test_collector_skips_without_query() -> None:
    settings = Settings(google_places_api_key=SecretStr("test-key"))
    result = collect_google_business_facts(settings, query="   ")
    assert result["status"] == "skipped"
    assert result["reason"] == "no_business_query"


def _ig_facts_with_reviews(review_count):
    from datetime import UTC, datetime

    from apps.worker.stages.social.extractor import extract_social_facts

    raw = json.loads((FIXTURES / "social_instagram_strong.json").read_text())
    facts = extract_social_facts(
        [{"platform": "instagram", "handle": "acme", "raw": raw}],
        now=datetime(2026, 6, 23, tzinfo=UTC),
    )
    if review_count is not None:
        facts["summary"]["google_review_count"] = review_count
    return facts


def test_google_reviews_rule_scores_when_present() -> None:
    from apps.worker.stages.scoring import score_social_audit

    result = score_social_audit(_ig_facts_with_reviews(128), Settings())
    by_id = {r["rule_id"]: r for r in result["category"]["rules"]}
    assert by_id["social.reputation.google_reviews"]["result"] == "pass"


def test_google_reviews_rule_skips_when_absent() -> None:
    # No Google listing (keyless/standalone) -> review count None -> the rule skip-rescales,
    # so a v3-equivalent audit scores identically (no calibration drift).
    from apps.worker.stages.scoring import score_social_audit

    result = score_social_audit(_ig_facts_with_reviews(None), Settings())
    by_id = {r["rule_id"]: r for r in result["category"]["rules"]}
    assert by_id["social.reputation.google_reviews"]["result"] == "skipped"


def _collect_with_listing(monkeypatch, website, *, expected_url):
    # Text Search is a fuzzy name lookup — these tests pin the identity gate that keeps a
    # same-named stranger's listing (reviews/phone) out of a client-facing report.
    from apps.worker.stages.social import places_provider

    settings = Settings(_env_file=None, google_places_api_key=SecretStr("test-key"))
    monkeypatch.setattr(places_provider, "fetch_place_id", lambda query, s: "place-1")
    payload = {"displayName": {"text": "Acme"}, "userRatingCount": 3}
    if website is not None:
        payload["websiteUri"] = website
    monkeypatch.setattr(places_provider, "fetch_place_details", lambda pid, s: payload)
    return places_provider.collect_google_business_facts(
        settings, query="acme", expected_url=expected_url
    )


def test_collector_rejects_listing_for_another_website(monkeypatch) -> None:
    result = _collect_with_listing(
        monkeypatch, "https://someoneelse.example/", expected_url="https://www.acme.com/"
    )
    assert result["status"] == "failed"
    assert result["reason"] == "website_mismatch"


def test_collector_rejects_listing_without_website(monkeypatch) -> None:
    # No website on the listing = unverifiable = never attributed to the client.
    result = _collect_with_listing(monkeypatch, None, expected_url="https://www.acme.com/")
    assert result["status"] == "failed"
    assert result["reason"] == "website_mismatch"


def test_collector_accepts_listing_matching_the_audited_site(monkeypatch) -> None:
    result = _collect_with_listing(
        monkeypatch, "https://acme.com/contact", expected_url="https://www.acme.com/"
    )
    assert result["status"] == "complete"
    assert result["business"]["review_count"] == 3


def test_collector_accepts_subdomain_relationship(monkeypatch) -> None:
    # Audited shop.acme.com <-> listing website acme.com: same business, different depth.
    result = _collect_with_listing(
        monkeypatch, "https://www.acme.com/", expected_url="https://shop.acme.com/page"
    )
    assert result["status"] == "complete"


def test_collector_rejects_platform_apex_relationship(monkeypatch) -> None:
    # foo.wixsite.com audited vs a listing whose website is wixsite.com (the platform — or a
    # stranger tenant's apex): a suffix rule would attribute someone else's listing to the
    # client, so the multi-tenant apex relationship must NOT match.
    result = _collect_with_listing(
        monkeypatch,
        "https://www.wixsite.com/",
        expected_url="https://smithbuilders.wixsite.com/home",
    )
    assert result["status"] == "failed"
    assert result["reason"] == "website_mismatch"


def test_collector_path_tenant_host_requires_matching_path(monkeypatch) -> None:
    # sites.google.com hosts many businesses distinguished by PATH: same host is not enough.
    stranger = _collect_with_listing(
        monkeypatch,
        "https://sites.google.com/view/stranger",
        expected_url="https://sites.google.com/view/acme",
    )
    assert stranger["status"] == "failed"
    assert stranger["reason"] == "website_mismatch"
    own = _collect_with_listing(
        monkeypatch,
        "https://sites.google.com/view/acme",
        expected_url="https://sites.google.com/view/acme",
    )
    assert own["status"] == "complete"


def test_collector_accepts_root_listing_for_deep_audited_path(monkeypatch) -> None:
    # A deep page pasted as the audit URL (acme.com/home) must still match the listing's
    # root website on a normal (non-path-tenant) domain.
    result = _collect_with_listing(
        monkeypatch, "https://acme.com/", expected_url="https://www.acme.com/home"
    )
    assert result["status"] == "complete"


def test_collector_without_expected_url_keeps_legacy_behavior(monkeypatch) -> None:
    result = _collect_with_listing(monkeypatch, "https://someoneelse.example/", expected_url=None)
    assert result["status"] == "complete"


def test_website_match_rejects_bare_public_suffix_apex() -> None:
    # A listing website that is a bare public suffix "shares an apex" with the audited site but
    # carries no brand label — it can never establish that the two are the same business.
    from apps.worker.stages.social.places_provider import _website_matches

    assert _website_matches("https://co.uk/", "https://acme.co.uk/") is False
    assert _website_matches("https://com/", "https://acme.com/") is False
    # A genuine ccTLD apex relationship still matches (its brand label is "acme").
    assert _website_matches("https://shop.acme.co.uk/", "https://acme.co.uk/") is True
