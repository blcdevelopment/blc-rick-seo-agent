from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.shared.audit_states import AuditStatus
from apps.shared.models import AuditJob, AuditResult, Base


def test_audit_job_and_result_can_be_persisted() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with Session() as db:
        job = AuditJob(
            url="https://example.com/",
            niche="builder",
            target_audience="homeowners",
            status=AuditStatus.QUEUED.value,
            current_stage="Queued",
            progress_pct=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        assert job.id is not None
        assert job.status == AuditStatus.QUEUED.value

        job.status = AuditStatus.COMPLETE.value
        job.current_stage = "Placeholder audit complete"
        job.progress_pct = 100
        db.add(
            AuditResult(
                job_id=job.id,
                seo_score=0,
                uxui_score=0,
                lead_gen_score=0,
                crawled_pages={"pages": []},
                seo_facts={},
                uxui_facts={},
                psi_facts={},
                score_breakdown={},
                commentary={},
                validation_log={"unsupported_claims": []},
                report_metadata={"renderer": "not_configured"},
                pdf_path=None,
                rubric_version="phase-1-placeholder",
                llm_model="not_configured",
            )
        )
        db.commit()
        db.refresh(job)

        assert job.result is not None
        assert job.result.lead_gen_score == 0
        assert job.result.report_metadata["renderer"] == "not_configured"
