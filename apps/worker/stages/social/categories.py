"""Niche -> acceptable-business-category taxonomy + matcher (SAE-4 / completes SAE-9).

Elda's review asks us to check a business is listed under the RIGHT category on Facebook/
Instagram/Google (contractor, remodeler, interior designer, ...), not merely that *a* category
is set. This maps the audit's free-text ``niche`` to a family of acceptable category strings and
answers "does this profile's category fit the niche?".

Design for safety (this is client-facing): the matcher returns ``None`` — meaning "can't tell,
skip the rule" — whenever the niche can't be confidently classified, so a vague niche never
produces a false "wrong category" finding. Only a niche we recognize AND a category we can compare
yields True/False.

Confirmed by Elda (2026-07-20): BLC's typical clients fall under the Instagram "Real Estate & Home
Services" group as **Architectural Designer / Contractor / Home Improvement / Landscape** (all
matched by ``acceptable_categories`` below), while — per her explicit "with the exception of the
real estate categories and cleaning" note — the **Real Estate (Agent / Company / Property
Management)** and **Cleaning Service** categories are NOT appropriate for a builder/remodeler and
are flagged as a mismatch via ``misfit_categories``.
"""

from __future__ import annotations

JsonDict = dict[str, object]

# family -> {niche keyword fragments that select it, acceptable category substrings}.
# Matching is case-insensitive substring containment on both sides (categories differ across
# Instagram businessCategoryName / Facebook category / Google types, so exact equality is brittle).
NICHE_CATEGORY_FAMILIES: dict[str, dict[str, tuple[str, ...]]] = {
    "home_services": {
        "niche_keywords": (
            "home",
            "house",
            "build",
            "construct",
            "contractor",
            "remodel",
            "renovat",
            "roof",
            "kitchen",
            "bath",
            "interior",
            "deck",
            "landscap",
            "hvac",
            "plumb",
            "electric",
            "concrete",
            "fenc",
            "flooring",
            "cabinet",
        ),
        "acceptable_categories": (
            "contractor",
            "builder",
            "construction",
            "remodel",
            "renovation",
            "interior design",
            "home improvement",
            "roofing",
            "landscap",
            "architect",
            "kitchen",
            "bathroom",
            "handyman",
            "carpenter",
            "hvac",
            "plumb",
            "electric",
            "flooring",
            "cabinet",
            "deck",
            "fence",
        ),
        # Elda's "exception": categories that are WRONG for a builder/remodeler client (they belong
        # to adjacent-but-different trades). A home-services niche listed under one of these is a
        # confident mismatch, not an ambiguous skip. Kept specific (e.g. "real estate agent", not
        # bare "real estate") so a legitimate "Real Estate Developer" builder isn't false-flagged.
        "misfit_categories": (
            "real estate agent",
            "real estate company",
            "real estate broker",
            "realtor",
            "realty",
            "property management",
            "cleaning service",
            "janitorial",
            "maid service",
        ),
    },
}


def _classify_niche(niche: str | None) -> str | None:
    """The category family a niche belongs to, or None when we can't confidently classify it."""
    text = (niche or "").lower()
    if not text.strip():
        return None
    for family, spec in NICHE_CATEGORY_FAMILIES.items():
        if any(keyword in text for keyword in spec["niche_keywords"]):
            return family
    return None


# Placeholder/generic categories that are safe to flag as "wrong" for a known niche. A SPECIFIC
# but different category (e.g. "Marketing Agency") is NOT flagged — we can't confidently call a real
# category wrong from a coarse niche, and doing so produces false findings (a marketing agency that
# serves home builders is correctly "Marketing Agency", not "Home Builder").
_GENERIC_CATEGORIES = ("local business", "business", "page", "local", "company", "website")


def category_matches_niche(niche: str | None, category: str | None) -> bool | None:
    """Whether ``category`` fits the business ``niche``.

    Returns True when the category matches the niche family, False ONLY when the category is
    generic/placeholder (so a clear "set a specific category" nudge is warranted), and None
    (=> the rule skips) when the niche is unclassifiable, no category is set, OR the category is a
    specific-but-different one — because we can't confidently call a real category wrong, and doing
    so produced false "wrong category" findings.
    """
    family = _classify_niche(niche)
    if family is None or not category or not category.strip():
        return None
    cat = category.lower()
    spec = NICHE_CATEGORY_FAMILIES[family]
    if any(token in cat for token in spec["acceptable_categories"]):
        return True
    # Expert-flagged wrong-for-this-niche categories (Elda) -> a confident mismatch, not a skip.
    if any(token in cat for token in spec.get("misfit_categories", ())):
        return False
    if any(generic in cat for generic in _GENERIC_CATEGORIES):
        return False
    # Specific but different category -> ambiguous -> skip rather than false-flag.
    return None


def category_relevance(niche: str | None, categories: list[str | None]) -> bool | None:
    """Roll up per-category matches for a business: True if any declared category fits the niche,
    False if categories are set but none fit, None if nothing is comparable (rule skips)."""
    verdicts = [category_matches_niche(niche, c) for c in categories]
    verdicts = [v for v in verdicts if v is not None]
    if not verdicts:
        return None
    return any(verdicts)
