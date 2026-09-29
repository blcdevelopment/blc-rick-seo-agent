"""Niche -> category matcher (SAE-4 / SAE-9-full).

NOTE: the home-services taxonomy in categories.py is Elda-confirmed (2026-07-20); these tests pin
the *behaviour* (conservative skip-on-unknown, plus her explicit real-estate/cleaning exceptions).
"""

from apps.worker.stages.social.categories import category_matches_niche, category_relevance


def test_matching_category_for_home_services_niche() -> None:
    assert category_matches_niche("homes", "General Contractor") is True
    assert category_matches_niche("home builder", "Home Builder") is True
    assert category_matches_niche("kitchen remodeling", "Kitchen Remodeler") is True


def test_generic_category_is_flagged_false() -> None:
    # A recognized niche + a GENERIC/placeholder category -> False (a real "set a specific one").
    assert category_matches_niche("remodeling", "Local Business") is False
    assert category_matches_niche("home builder", "Page") is False


def test_specific_but_different_category_is_skipped_not_false() -> None:
    # A specific real category that just isn't home-services (e.g. a marketing agency) is ambiguous
    # -> None (skip), NOT a false "wrong category" finding. This is the BLC-audits-itself case.
    assert category_matches_niche("home builder", "Marketing Agency") is None
    assert category_matches_niche("homes", "Restaurant") is None


def test_elda_excepted_categories_are_flagged_for_builder_niche() -> None:
    # Per Elda's "exception of the real estate categories and cleaning": a builder/remodeler listed
    # under Real Estate / Property Management / Cleaning Service is a CONFIDENT mismatch (False),
    # not an ambiguous skip.
    assert category_matches_niche("home builder", "Real Estate Agent") is False
    assert category_matches_niche("remodeling", "Real Estate Company") is False
    assert category_matches_niche("home builder", "Property Management Company") is False
    assert category_matches_niche("home builder", "Cleaning Service") is False
    # Kept specific: a builder that is ALSO a developer isn't false-flagged (no "agent/company"
    # token in "Real Estate Developer"), so it stays an ambiguous skip.
    assert category_matches_niche("home builder", "Real Estate Developer") is None


def test_unclassifiable_niche_returns_none() -> None:
    # Conservative: an unknown/blank niche can't be judged -> None -> the rule skips (no guess).
    assert category_matches_niche("crypto newsletter", "Contractor") is None
    assert category_matches_niche("", "Contractor") is None
    assert category_matches_niche(None, "Contractor") is None


def test_missing_category_returns_none() -> None:
    assert category_matches_niche("home builder", None) is None
    assert category_matches_niche("home builder", "") is None


def test_relevance_rollup_any_match_wins() -> None:
    # At least one profile categorized relevantly -> True.
    assert category_relevance("homes", ["Local Business", "General Contractor"]) is True
    # A generic category with no relevant match -> False.
    assert category_relevance("home builder", ["Local Business"]) is False
    # Only specific-but-different categories -> None (skip), not a false "wrong" (BLC's own case).
    assert category_relevance("home builder", ["Marketing Agency", "Entrepreneur"]) is None
    # Nothing comparable (unknown niche, or no categories) -> None.
    assert category_relevance("mystery niche", ["Contractor"]) is None
    assert category_relevance("homes", [None, ""]) is None
