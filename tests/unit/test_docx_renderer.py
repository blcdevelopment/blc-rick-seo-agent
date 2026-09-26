from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

from apps.worker.stages.docx_renderer import render_report_docx
from apps.worker.stages.report_payload import compose_report_payload


def test_render_report_docx_writes_valid_package(tmp_path) -> None:
    payload = compose_report_payload(_job(), _result())
    output_path = tmp_path / "audit.docx"

    result = render_report_docx(payload, output_path=output_path)

    assert result.docx_path == str(output_path)
    assert result.size_bytes > 0
    with ZipFile(output_path) as archive:
        names = set(archive.namelist())
        assert "[Content_Types].xml" in names
        assert "word/document.xml" in names
        document = archive.read("word/document.xml").decode("utf-8")

    assert "Website Audit Report" in document
    assert "Executive Summary" in document
    assert "How to use it" in document
    assert "Site Health" in document
    assert "CTR (click-through rate)" in document
    assert "Google Search Performance" in document
    assert "Lead Generation Roadmap" in document


def test_render_report_docx_appends_benchmark_section(tmp_path) -> None:
    # When a benchmark ran, the DOCX must append the Competitor Benchmarking section too (parity
    # with the PDF) — otherwise the same audit's PDF and DOCX diverge.
    result = _result()
    result.score_breakdown = {
        **result.score_breakdown,
        "benchmark": {
            "status": "complete",
            "provider": "semrush",
            "competitors": [
                {"label": "rival.com", "is_industry": False, "source": "semrush", "seo": 60},
            ],
        },
    }
    payload = compose_report_payload(_job(), result)
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Competitor Benchmarking" in document
    assert "rival.com" in document
    assert "Benchmark source: semrush" in document


def test_render_report_docx_omits_benchmark_when_absent(tmp_path) -> None:
    # No benchmark => no section (byte-identical to a plain website DOCX).
    payload = compose_report_payload(_job(), _result())
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Competitor Benchmarking" not in document


def test_render_report_docx_appends_social_depth_sections(tmp_path) -> None:
    # A combined audit's DOCX must carry the same social depth as the PDF: quantified findings,
    # strengths, content insights, and top posts.
    payload = compose_report_payload(_job(), _combined_result())
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")

    assert "Social Media Audit" in document
    # Quantified finding: label plus the measured-vs-target metric line (em-dash, not nested
    # parens — the metric itself already contains a parenthesised target).
    assert "Infrequent posting — 1.3 (target ≥ 8)" in document
    assert "Post at least twice a week." in document
    # Strengths.
    assert "What's working" in document
    assert "Profiles are complete" in document
    # Content insights (same non-None field set the PDF renders).
    assert "Content insights" in document
    assert "Content mix: video 20.0%, image 60.0%, carousel 20.0%" in document
    assert "Avg views per video: 850.0" in document
    assert "Likes per comment: 12.0" in document
    assert "Follower/following ratio: 3.2x" in document
    # Top posts.
    assert "Top performing posts" in document
    assert "Deck build timelapse" in document
    assert "views=900" in document
    assert "engagement=45" in document
    # Per-platform row extended with video-share/business columns.
    assert "video share=20.0%" in document
    assert "business=yes" in document


def test_render_report_docx_omits_social_sections_when_absent(tmp_path) -> None:
    # Website-only audit => none of the social depth sections appear.
    payload = compose_report_payload(_job(), _result())
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Social Media Audit" not in document
    assert "What's working" not in document
    assert "Content insights" not in document
    assert "Top performing posts" not in document


def test_docx_renders_legacy_search_window_without_none_days(tmp_path) -> None:
    # GSC facts stored before the window facts existed have no "days" key; the on-demand
    # DOCX of such an audit must render the bare range, never "(None days)".
    result = _result()
    result.external_seo_facts = {
        "gsc": {
            "status": "complete",
            "site_url": "https://example.com/",
            "date_range": {"start": "2026-01-01", "end": "2026-03-31"},
        }
    }
    payload = compose_report_payload(_job(), result)
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "None days" not in document
    assert "Data window: 2026-01-01 to 2026-03-31." in document


def test_docx_website_only_overall_keeps_website_title(tmp_path) -> None:
    # A combined-typed job whose social collection failed stores a website_only overall
    # (real score, zero social data); the title must not promise a social section.
    result = _result()
    result.score_breakdown = {
        **result.score_breakdown,
        "overall_readiness": {
            "status": "website_only",
            "rubric_version": "phase2-overall-v1",
            "score": 72,
            "band": "fair",
            "max_score": 100,
            "weights": {"website": 1.0, "social": 0.0},
            "inputs": {"website_lead_gen": 72, "social": None},
        },
    }
    payload = compose_report_payload(_job(), result)
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Website &amp; Social Media Audit Report" not in document
    assert "Website Audit Report" in document
    # The score card must not point the reader at a social section that doesn't exist.
    lead_gen_card = next(card for card in payload.scores if card.id == "lead_gen")
    assert lead_gen_card.label == "Lead Generation Readiness"
    assert "reported at the end of this report" not in lead_gen_card.description


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        url="https://example.com/",
        niche="builder",
        target_audience="homeowners",
    )


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        seo_score=76,
        uxui_score=68,
        lead_gen_score=72,
        crawled_pages={
            "status": "complete",
            "requested_url": "https://example.com/",
            "final_url": "https://example.com/",
            "summary": {"successful_pages": 2, "failed_pages": 0, "skipped_pages": 0},
        },
        seo_facts={},
        uxui_facts={},
        psi_facts={"status": "skipped", "summary": {}},
        score_breakdown={"scores": {"seo": 76, "uxui": 68, "lead_gen": 72}},
        commentary={"content": {"executive_summary": "The site has clear opportunities."}},
        validation_log={"status": "complete"},
        report_metadata={},
        pdf_path=None,
        rubric_version="phase1-test",
        llm_model="deterministic",
    )


def _combined_result() -> SimpleNamespace:
    result = _result()
    result.social_score = 55
    result.social_facts = {
        "status": "complete",
        "summary": {
            "platforms_audited": 1,
            "total_followers": 1200,
            "video_share_pct": 20.0,
            "image_share_pct": 60.0,
            "carousel_share_pct": 20.0,
            "total_views": 3400,
            "avg_views_per_post": 850.0,
            "avg_engagement_rate_pct": 0.8,
            "avg_like_to_comment_ratio": 12.0,
            "max_posting_gap_days": 40,
            "avg_hashtags_per_post": 2.0,
            "posts_with_cta_caption_pct": 25.0,
            "avg_follower_following_ratio": 3.2,
        },
        "platforms": [
            {
                "platform": "instagram",
                "handle": "acme",
                "status": "complete",
                "followers": 1200,
                "posts_per_month": 1.3,
                "days_since_last_post": 40,
                "avg_engagement_rate_pct": 0.8,
                "video_share_pct": 20.0,
                "is_business": True,
                "top_posts": [
                    {
                        "type": "video",
                        "views": 900,
                        "likes": 40,
                        "comments": 5,
                        "engagement": 45,
                        "posted": "2026-06-01",
                        "title": "Deck build timelapse",
                    }
                ],
            }
        ],
    }
    result.score_breakdown = {
        **result.score_breakdown,
        "social": {
            "score": 55,
            "category": {
                "rules": [
                    {
                        "rule_id": "social.cadence",
                        "result": "fail",
                        "finding_label": "Infrequent posting",
                        "remediation": "Post at least twice a week.",
                        "impact": "high",
                        "tier": "quick_win",
                        "fact_path": "social.summary.avg_posts_per_month",
                        "evidence": {"value": 1.3, "params": {"min": 8}},
                    },
                    {
                        "rule_id": "social.profile.complete",
                        "result": "pass",
                        "description": "Profiles are complete (bio, link, contact)",
                    },
                ]
            },
        },
        "overall_readiness": {
            "status": "complete",
            "rubric_version": "phase2-overall-v1",
            "score": 66,
            "band": "fair",
            "max_score": 100,
            "weights": {"website": 0.7, "social": 0.3},
            "inputs": {"website_lead_gen": 72, "social": 55},
        },
    }
    return result


def test_render_report_docx_falls_back_to_recommendations_for_legacy_commentary(
    tmp_path,
) -> None:
    # Audits stored BEFORE the finding-card rework carry findings without action_items; their
    # fixes live only in the per-section recommendations, which the DOCX must then render
    # (mirrors the web UI's fallback) — otherwise an on-demand DOCX of an old audit prints
    # problems with no "do this" anywhere.
    result = _result()
    result.commentary = {
        "content": {
            "executive_summary": "Legacy summary.",
            "seo": {
                "summary": "Legacy SEO summary.",
                "findings": [
                    {
                        "severity": "high",
                        "title": "Missing titles",
                        "explanation": "Several pages lack title tags.",
                    }
                ],
                "recommendations": [
                    {
                        "tier": "quick_win",
                        "title": "Write unique title tags",
                        "rationale": "Titles drive rankings and clicks.",
                        "action_items": ["Add a unique title to every page."],
                    }
                ],
            },
        }
    }
    payload = compose_report_payload(_job(), result)
    output_path = tmp_path / "legacy.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert ">Recommendations<" in document
    assert "Titles drive rankings and clicks." in document
    assert "Add a unique title to every page." in document


def test_docx_overall_weights_round_half_up_like_the_ui(tmp_path) -> None:
    # Python round() is banker's (round(70.5) -> 70) while the UI uses Math.round (-> 71): a
    # 0.705/0.295 rubric must print the SAME percentages on both surfaces (half-up convention).
    result = _result()
    result.social_score = 55
    result.score_breakdown = {
        **result.score_breakdown,
        "overall_readiness": {
            "status": "complete",
            "rubric_version": "phase2-overall-v1",
            "score": 67,
            "band": "fair",
            "max_score": 100,
            "weights": {"website": 0.705, "social": 0.295},
            "inputs": {"website_lead_gen": 72, "social": 55},
        },
    }
    payload = compose_report_payload(_job(), result)
    output_path = tmp_path / "audit.docx"

    render_report_docx(payload, output_path=output_path)

    with ZipFile(output_path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "(weight 71%)" in document  # banker's round() would print 70%
    assert "(weight 30%)" in document
