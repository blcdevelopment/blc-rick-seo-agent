from apps.worker.stages.grounding_validator import (
    collect_social_known_numbers,
    social_text_has_ungrounded,
    validate_commentary_grounding,
)


def _commentary(summary: str) -> dict:
    return {
        "status": "complete",
        "provider": "test",
        "model": "test-model",
        "content": {
            "executive_summary": summary,
            "seo": {
                "headline": "SEO score is 80",
                "findings": [
                    {
                        "severity": "info",
                        "title": "SEO score is 80",
                        "explanation": "The SEO score is 80.",
                        "evidence_refs": ["scores.seo"],
                    }
                ],
                "recommendations": [
                    {
                        "tier": "quick_win",
                        "title": "Update meta descriptions",
                        "rationale": "The score breakdown flagged missing descriptions.",
                        "action_items": ["Add descriptions to crawled pages."],
                    }
                ],
            },
            "uxui": {
                "headline": "UX score is 70",
                "findings": [
                    {
                        "severity": "medium",
                        "title": "UX score is 70",
                        "explanation": "The UX score is 70.",
                        "evidence_refs": ["scores.uxui"],
                    }
                ],
                "recommendations": [
                    {
                        "tier": "quick_win",
                        "title": "Improve calls to action",
                        "rationale": "The score breakdown flagged CTA gaps.",
                        "action_items": ["Add a primary CTA."],
                    }
                ],
            },
            "lead_generation": {
                "headline": "Lead score is 75",
                "findings": [
                    {
                        "severity": "medium",
                        "title": "Lead score is 75",
                        "explanation": "The Lead Generation Readiness score is 75.",
                        "evidence_refs": ["composite"],
                    }
                ],
                "recommendations": [
                    {
                        "tier": "quick_win",
                        "title": "Fix failed rules",
                        "rationale": "The score breakdown shows deterministic gaps.",
                        "action_items": ["Resolve failed rules first."],
                    }
                ],
            },
        },
    }


def test_grounding_validator_keeps_supported_numeric_claims() -> None:
    sanitized, log = validate_commentary_grounding(
        _commentary("SEO score is 80. UX score is 70. Lead score is 75."),
        fact_sources={"scores": {"seo": 80, "uxui": 70, "lead_gen": 75}},
    )

    assert log["unsupported_claim_count"] == 0
    assert sanitized["content"]["executive_summary"] == (
        "SEO score is 80. UX score is 70. Lead score is 75."
    )


def test_grounding_validator_strips_unsupported_numeric_claims() -> None:
    sanitized, log = validate_commentary_grounding(
        _commentary("SEO score is 80. The site has 42 missing pages."),
        fact_sources={"scores": {"seo": 80, "uxui": 70, "lead_gen": 75}},
    )

    assert log["unsupported_claim_count"] == 1
    assert "42 missing pages" not in sanitized["content"]["executive_summary"]
    assert sanitized["content"]["executive_summary"] == "SEO score is 80."


def test_grounding_validator_reverts_to_baseline_instead_of_placeholder() -> None:
    # The whole field is an unsupported numeric claim, so stripping would empty it. The
    # validator must revert to the original text, never leak a placeholder string.
    summary = "The site has 42 missing pages."
    sanitized, log = validate_commentary_grounding(
        _commentary(summary),
        fact_sources={"scores": {"seo": 80, "uxui": 70, "lead_gen": 75}},
    )

    result = sanitized["content"]["executive_summary"]
    assert "grounding validator" not in result.lower()
    assert result == summary
    # The revert is recorded honestly: the flagged claim is counted and the action is
    # labelled as a baseline revert, not a strip (the text was kept, not removed).
    assert log["unsupported_claim_count"] == 1
    assert log["action"] == "reverted_unsupported_to_baseline"


def test_grounding_validator_does_not_strip_advice_numbers_in_action_items() -> None:
    # action_items are prescriptive advice and evidence_refs are machine identifiers; the
    # numbers in them are not factual claims about the site, so grounding leaves them alone.
    commentary = _commentary("SEO score is 80.")
    advice = "Add a unique 70-160 character meta description to every page."
    commentary["content"]["seo"]["recommendations"][0]["action_items"] = [advice]
    commentary["content"]["seo"]["findings"][0]["evidence_refs"] = ["seo.pages[0].meta_description"]

    sanitized, log = validate_commentary_grounding(
        commentary,
        fact_sources={"scores": {"seo": 80, "uxui": 70, "lead_gen": 75}},
    )

    assert sanitized["content"]["seo"]["recommendations"][0]["action_items"] == [advice]
    assert sanitized["content"]["seo"]["findings"][0]["evidence_refs"] == [
        "seo.pages[0].meta_description"
    ]
    assert log["unsupported_claim_count"] == 0


def test_grounding_validator_keeps_timeframe_language() -> None:
    summary = "Resolve the top issues within 30 days and plan structural work over 1-3 months."
    sanitized, log = validate_commentary_grounding(
        _commentary(summary),
        fact_sources={"scores": {"seo": 80, "uxui": 70, "lead_gen": 75}},
    )

    # Timeframe numbers are rhetorical, not measured facts, so the sentence is preserved.
    assert log["unsupported_claim_count"] == 0
    assert sanitized["content"]["executive_summary"] == summary


# --- Shared social grounding (same module backs the social commentary backstop, SMWA-76) ---


def test_social_grounding_collects_known_numbers() -> None:
    known = collect_social_known_numbers({"summary": {"total_followers": 5000}})
    assert "5000" in known


def test_social_grounding_flags_fabricated_but_keeps_grounded_and_advice() -> None:
    known = collect_social_known_numbers({"summary": {"total_followers": 5000}})
    # A fabricated percentage not in the facts is flagged.
    assert social_text_has_ungrounded("Studies show 80% ignore inactive pages.", known) is True
    # A real follower count from the facts is kept.
    assert social_text_has_ungrounded("With 5000 followers you have real reach.", known) is False
    # Small advice numbers (cadence) are not claims, so they are never flagged.
    assert social_text_has_ungrounded("Post 2-3 times per week.", known) is False
