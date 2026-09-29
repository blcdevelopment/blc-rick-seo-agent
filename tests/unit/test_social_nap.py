"""Website<->social phone (NAP) cross-check + Google Business enrichment in the combined path
(SAE-10/13)."""

from types import SimpleNamespace

from apps.shared.config import Settings
from apps.worker import tasks as tasks_mod
from apps.worker.tasks import (
    _augment_with_google_business,
    _business_query,
    _inject_nap_consistency,
)


def _result(phone_numbers: list[str]) -> SimpleNamespace:
    # Minimal stand-in for AuditResult.uxui_facts (extractor_uxui shape: pages[].contact).
    return SimpleNamespace(uxui_facts={"pages": [{"contact": {"phone_numbers": phone_numbers}}]})


def _facts(social_phone) -> dict:
    return {"summary": {}, "platforms": [{"phone": social_phone}]}


def test_matching_phone_sets_consistent_true() -> None:
    facts = _facts("(555) 100-2000")
    _inject_nap_consistency(facts, _result(["555-100-2000"]))  # same number, different formatting
    assert facts["summary"]["nap_phone_consistent"] is True


def test_mismatched_phone_sets_consistent_false() -> None:
    facts = _facts("555-999-8888")
    _inject_nap_consistency(facts, _result(["555-100-2000"]))
    assert facts["summary"]["nap_phone_consistent"] is False


def test_no_social_phone_leaves_none() -> None:
    facts = _facts(None)  # e.g. an Instagram-only audit (IG returns no phone)
    _inject_nap_consistency(facts, _result(["555-100-2000"]))
    assert facts["summary"].get("nap_phone_consistent") is None


def test_no_website_phone_leaves_none() -> None:
    facts = _facts("555-100-2000")
    _inject_nap_consistency(facts, _result([]))
    assert facts["summary"].get("nap_phone_consistent") is None


def test_google_business_phone_participates_in_nap() -> None:
    # SAE-13: even an Instagram-only audit (IG returns no phone) can match on the Google listing.
    facts = {
        "summary": {},
        "platforms": [{"phone": None}],
        "google_business": {"phone": "555-100-2000"},
    }
    _inject_nap_consistency(facts, _result(["(555) 100-2000"]))
    assert facts["summary"]["nap_phone_consistent"] is True


def test_business_query_from_domain() -> None:
    assert (
        _business_query(SimpleNamespace(url="https://www.builderleadconverter.com/"))
        == "builderleadconverter"
    )
    assert _business_query(SimpleNamespace(url="https://acme-builders.example")) == "acme-builders"
    # The registrable label, not the first hostname label: a subdomain site must not query
    # Places for "shop", and a generic second-level ccTLD label must not query for "co".
    assert _business_query(SimpleNamespace(url="https://shop.acme.com/")) == "acme"
    assert _business_query(SimpleNamespace(url="https://smith.co.uk/")) == "smith"
    # Multi-tenant platform hosts carry the brand in the SUBDOMAIN, not the registrable label.
    assert (
        _business_query(SimpleNamespace(url="https://smithbuilders.wixsite.com/home"))
        == "smithbuilders"
    )


def test_augment_google_business_injects_signals(monkeypatch) -> None:
    monkeypatch.setattr(
        tasks_mod,
        "collect_google_business_facts",
        lambda settings, *, query, expected_url=None: {
            "status": "complete",
            "source": "google_business",
            "business": {"phone": "555-100-2000", "rating": 4.7, "review_count": 128},
        },
    )
    facts = {"summary": {}, "platforms": []}
    _augment_with_google_business(
        facts, Settings(), query="acme", expected_url="https://www.acme.com/"
    )
    assert facts["google_business"]["rating"] == 4.7
    assert facts["summary"]["google_review_count"] == 128
    assert facts["summary"]["google_rating"] == 4.7


def test_augment_google_business_skips_without_key() -> None:
    # Real collector, no key -> skipped -> no mutation (report stays byte-identical).
    # _env_file=None isolates from the local .env (which now carries a real Places key).
    facts = {"summary": {}, "platforms": []}
    _augment_with_google_business(
        facts, Settings(_env_file=None), query="acme", expected_url="https://www.acme.com/"
    )
    assert "google_business" not in facts
    assert facts["summary"].get("google_review_count") is None
