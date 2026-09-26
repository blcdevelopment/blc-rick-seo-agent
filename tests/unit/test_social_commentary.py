from types import SimpleNamespace

from apps.shared.config import Settings
from apps.worker.stages import commentary as commentary_mod
from apps.worker.stages.commentary import (
    SocialCommentaryContent,
    SocialCommentaryFinding,
    generate_social_commentary,
)
from apps.worker.stages.social.report import compose_social_report_payload

_FINDINGS = [
    {
        "id": "social.posting.cadence",
        "label": "Infrequent posting",
        "remediation": "Publish ~2+ posts/week.",
        "impact": "high",
        "tier": "mid_term",
        "result": "fail",
    }
]


def _settings(**overrides) -> Settings:
    base = {"_env_file": None, "openai_api_key": None}
    base.update(overrides)
    return Settings(**base)


def test_no_key_returns_deterministic_baseline() -> None:
    out = generate_social_commentary(
        audit_context={"handles": {"instagram": "acme"}},
        social_facts={"status": "complete", "summary": {}},
        score=70,
        findings=_FINDINGS,
        settings=_settings(),
    )
    assert out["provider"] == "deterministic"
    content = out["content"]
    assert "70/100" in content["executive_summary"]
    # Deterministic narrative is the vetted rule remediation.
    assert content["findings"][0]["id"] == "social.posting.cadence"
    assert content["findings"][0]["narrative"] == "Publish ~2+ posts/week."


def test_llm_fabricated_stat_falls_back_to_deterministic(monkeypatch) -> None:
    def _fake_call(**_kwargs) -> SocialCommentaryContent:
        return SocialCommentaryContent(
            executive_summary="Solid presence.",
            findings=[
                SocialCommentaryFinding(
                    id="social.posting.cadence",
                    title="Infrequent posting",
                    # 80% is not in the facts -> fabricated -> must be rejected.
                    narrative="Studies show 80% of homeowners ignore inactive pages.",
                )
            ],
        )

    monkeypatch.setattr(commentary_mod, "_call_openai_social", _fake_call)
    out = generate_social_commentary(
        audit_context={},
        social_facts={"status": "complete", "summary": {}},
        score=60,
        findings=_FINDINGS,
        settings=_settings(openai_api_key="sk-test"),
    )
    assert out["provider"] == "openai"
    # Fabricated narrative swapped back to the vetted deterministic remediation.
    assert out["content"]["findings"][0]["narrative"] == "Publish ~2+ posts/week."


def test_llm_grounded_number_is_kept(monkeypatch) -> None:
    def _fake_call(**_kwargs) -> SocialCommentaryContent:
        return SocialCommentaryContent(
            executive_summary="Good reach.",
            findings=[
                SocialCommentaryFinding(
                    id="social.posting.cadence",
                    title="Infrequent posting",
                    narrative="With 5000 followers you have real reach — post weekly to use it.",
                )
            ],
        )

    monkeypatch.setattr(commentary_mod, "_call_openai_social", _fake_call)
    out = generate_social_commentary(
        audit_context={},
        social_facts={"status": "complete", "summary": {"total_followers": 5000}},
        score=60,
        findings=_FINDINGS,
        settings=_settings(openai_api_key="sk-test"),
    )
    # 5000 IS in the facts, so the polished narrative is kept.
    assert "5000 followers" in out["content"]["findings"][0]["narrative"]


def test_report_payload_merges_commentary() -> None:
    result = SimpleNamespace(
        social_facts={"status": "complete", "summary": {"platforms_audited": 1}, "platforms": []},
        score_breakdown={
            "category": {
                "category": "social",
                "rules": [
                    {
                        "rule_id": "social.posting.cadence",
                        "result": "fail",
                        "finding_label": "Infrequent posting",
                        "remediation": "Post more.",
                        "impact": "high",
                        "tier": "mid_term",
                        "surface_as_finding": True,
                    }
                ],
            }
        },
        social_score=70,
        commentary={
            "provider": "openai",
            "content": {
                "executive_summary": "Good base, room to grow.",
                "findings": [
                    {"id": "social.posting.cadence", "narrative": "Aim for 2-3 uploads a week."}
                ],
            },
        },
    )
    job = SimpleNamespace(social_handles={"instagram": "acme"})
    payload = compose_social_report_payload(job, result)
    assert payload["executive_summary"] == "Good base, room to grow."
    assert payload["commentary_provider"] == "openai"
    assert payload["findings"][0]["narrative"] == "Aim for 2-3 uploads a week."


def test_report_payload_deterministic_when_no_commentary() -> None:
    result = SimpleNamespace(
        social_facts={"status": "complete", "summary": {"platforms_audited": 1}, "platforms": []},
        score_breakdown={
            "category": {
                "category": "social",
                "rules": [
                    {
                        "rule_id": "social.posting.cadence",
                        "result": "fail",
                        "finding_label": "Infrequent posting",
                        "remediation": "Post more.",
                        "impact": "high",
                        "tier": "mid_term",
                        "surface_as_finding": True,
                    }
                ],
            }
        },
        social_score=70,
        commentary=None,
    )
    job = SimpleNamespace(social_handles={"instagram": "acme"})
    payload = compose_social_report_payload(job, result)
    assert payload["executive_summary"] == ""
    assert payload["commentary_provider"] == "deterministic"
    assert payload["findings"][0]["narrative"] == ""
