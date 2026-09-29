from celery import Celery

from apps.shared.config import get_settings
from apps.shared.observability import init_sentry, quiet_http_client_logs

settings = get_settings()
init_sentry(settings, component="worker")
quiet_http_client_logs()

celery_app = Celery(
    "blc_website_audit_worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["apps.worker.tasks"],
)

celery_app.conf.update(
    accept_content=["json"],
    enable_utc=True,
    result_serializer="json",
    task_serializer="json",
    task_track_started=True,
    timezone="UTC",
    task_time_limit=settings.celery_task_time_limit_seconds,
    task_soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    # Recover work lost to a worker crash/restart (e.g. a mid-audit deploy) by REDELIVERING the
    # task instead of silently stranding the job in a non-terminal status. Safe because
    # run_collection_audit is idempotent — it no-ops on an already-COMPLETE job (see tasks.py).
    # A task that RAISES (incl. SoftTimeLimitExceeded) is still acked, so deterministic failures
    # are never redelivered; only a lost worker re-queues its in-flight task.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
