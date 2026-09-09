from celery import Celery

from app.config import settings

celery_app = Celery(
    "saas_backend",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.audit_tasks",
        "app.tasks.notification_tasks",
        "app.tasks.llm_tasks",
        "app.tasks.storage_tasks",
        "app.tasks.media_tasks",
    ],
)

celery_app.conf.update(
    task_routes={
        "app.tasks.audit_tasks.*": {"queue": "audit"},
        "app.tasks.notification_tasks.*": {"queue": "notifications"},
        "app.tasks.llm_tasks.ingest_document_task": {"queue": "llm_heavy"},
        "app.tasks.llm_tasks.*": {"queue": "llm"},
        "app.tasks.storage_tasks.scan_document_task": {"queue": "llm_heavy"},
        "app.tasks.storage_tasks.apply_lifecycle_policies_task": {"queue": "llm_heavy"},
        "app.tasks.storage_tasks.sweep_stale_multipart_uploads_task": {"queue": "llm_heavy"},
        "app.tasks.media_tasks.*": {"queue": "media"},
    },
    task_acks_late=True,               # redelivers on worker crash instead of silently dropping
    worker_prefetch_multiplier=1,      # fair dispatch — important for long-running LLM tasks
    task_default_retry_delay=10,
    result_expires=3600,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    "create-next-audit-partition": {
        "task": "app.tasks.audit_tasks.ensure_next_month_partition",
        "schedule": 60 * 60 * 24,  # daily; task itself is idempotent (CREATE TABLE IF NOT EXISTS)
    },
    "cleanup-expired-refresh-tokens": {
        "task": "app.tasks.notification_tasks.cleanup_expired_tokens",
        "schedule": 60 * 60 * 6,
    },
    "drop-expired-audit-partitions": {
        "task": "app.tasks.audit_tasks.drop_expired_audit_partitions",
        "schedule": 60 * 60 * 24,
    },
    "replay-dead-letter-audit-logs": {
        "task": "app.tasks.audit_tasks.replay_dead_letter_audit_logs",
        "schedule": 60 * 15,  # every 15 min — cheap no-op when the dead-letter list is empty
    },
    "apply-storage-lifecycle-policies": {
        "task": "app.tasks.storage_tasks.apply_lifecycle_policies_task",
        "schedule": 60 * 60 * 24,  # daily, off-peak in practice via a crontab schedule in production
    },
    "sweep-stale-multipart-uploads": {
        "task": "app.tasks.storage_tasks.sweep_stale_multipart_uploads_task",
        "schedule": 60 * 60 * 6,
    },
}
