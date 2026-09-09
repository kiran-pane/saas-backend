import json

from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.tasks.celery_app import celery_app


def _sync_engine():
    # A plain sync SQLAlchemy engine for Celery workers (psycopg2), kept
    # separate from the app's async engine — Celery's prefork worker model
    # doesn't play well with asyncio event loops per task.
    from sqlalchemy import create_engine

    from app.config import settings

    sync_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    return create_engine(sync_url, pool_pre_ping=True)


_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = _sync_engine()
    return _engine


_DEAD_LETTER_KEY = "audit:dead_letter"


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def write_audit_log(self, tenant_id: str, actor_id: str | None, action: str,
                     resource_type: str | None, resource_id: str | None,
                     metadata: dict, request_id: str | None = None):
    try:
        with _get_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_logs
                        (tenant_id, actor_user_id, action, resource_type, resource_id, request_id, audit_metadata)
                    VALUES (:tenant_id, :actor_id, :action, :resource_type, :resource_id, :request_id, :metadata)
                    """
                ),
                {
                    "tenant_id": tenant_id, "actor_id": actor_id, "action": action,
                    "resource_type": resource_type, "resource_id": resource_id,
                    "request_id": request_id, "metadata": metadata,
                },
            )
    except OperationalError as exc:
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            # A sustained Postgres outage shouldn't silently lose a
            # security-relevant audit event with only a stderr line as
            # evidence (that line won't survive standard log rotation in
            # most setups) — push to a Redis dead-letter list instead,
            # replayable via replay_dead_letter_audit_logs below.
            _push_to_dead_letter(tenant_id, actor_id, action, resource_type, resource_id, metadata, request_id)
            print(f"AUDIT LOG WRITE FAILED PERMANENTLY (queued to dead-letter): {action} tenant={tenant_id}")


def _push_to_dead_letter(tenant_id, actor_id, action, resource_type, resource_id, metadata, request_id) -> None:
    import redis as redis_sync
    from app.config import settings

    client = redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
    client.rpush(_DEAD_LETTER_KEY, json.dumps({
        "tenant_id": tenant_id, "actor_id": actor_id, "action": action,
        "resource_type": resource_type, "resource_id": resource_id,
        "metadata": metadata, "request_id": request_id,
    }))


@celery_app.task
def replay_dead_letter_audit_logs(max_items: int = 100):
    """Beat-scheduled: periodically retries audit writes that were
    permanently dead-lettered during an earlier outage. Idempotent in the
    sense that a replay failure just re-queues the item for the next run
    rather than losing it."""
    import redis as redis_sync
    from app.config import settings

    client = redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
    replayed = 0
    for _ in range(max_items):
        raw = client.lpop(_DEAD_LETTER_KEY)
        if raw is None:
            break
        item = json.loads(raw)
        try:
            with _get_engine().begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO audit_logs
                            (tenant_id, actor_user_id, action, resource_type, resource_id, request_id, audit_metadata)
                        VALUES (:tenant_id, :actor_id, :action, :resource_type, :resource_id, :request_id, :metadata)
                        """
                    ),
                    item,
                )
            replayed += 1
        except OperationalError:
            client.rpush(_DEAD_LETTER_KEY, raw)  # still down — put it back for the next run
            break
    return replayed


@celery_app.task
def ensure_next_month_partition():
    """Idempotent: creates next month's audit_logs partition if it
    doesn't already exist. Keeps the append-only audit table queryable
    and vacuum-able at scale (see blueprint section 2.3)."""
    with _get_engine().begin() as conn:
        conn.execute(
            text(
                """
                DO $$
                DECLARE
                    start_date date := date_trunc('month', now() + interval '1 month');
                    end_date date := start_date + interval '1 month';
                    partition_name text := 'audit_logs_' || to_char(start_date, 'YYYY_MM');
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = partition_name) THEN
                        EXECUTE format(
                            'CREATE TABLE %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
                            partition_name, start_date, end_date
                        );
                    END IF;
                END $$;
                """
            )
        )


@celery_app.task
def drop_expired_audit_partitions():
    """Drops audit_logs partitions older than
    settings.AUDIT_LOG_RETENTION_MONTHS. Dropping a whole partition is a
    fast metadata-only operation (unlike a row-by-row DELETE), which is
    exactly why the table is partitioned in the first place. A floor of
    2 months is enforced regardless of the configured value, so a
    misconfigured near-zero retention can't nuke the current/next
    month's live partition.

    Date parsing and the cutoff comparison happen in Python (not inside
    a PL/pgSQL DO block) — mixing bind parameters with dollar-quoted DO
    block bodies is fragile to reason about correctness-wise, and there's
    no need for server-side scripting here when Python can do the same
    logic more legibly.
    """
    import re
    from datetime import date

    from app.config import settings

    retention_months = max(settings.AUDIT_LOG_RETENTION_MONTHS, 2)
    today = date.today()
    cutoff_year, cutoff_month = today.year, today.month - retention_months
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1

    partition_pattern = re.compile(r"^audit_logs_(\d{4})_(\d{2})$")

    with _get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT inhrelid::regclass::text AS partition_name FROM pg_inherits "
                 "WHERE inhparent = 'audit_logs'::regclass")
        ).fetchall()
        for row in rows:
            match = partition_pattern.match(row.partition_name)
            if not match:
                continue  # doesn't match our naming scheme — never touch it
            year, month = int(match.group(1)), int(match.group(2))
            if (year, month) < (cutoff_year, cutoff_month):
                # partition_name is validated against partition_pattern
                # above (digits only, our own naming scheme) before ever
                # being interpolated — never derived from unvalidated input.
                conn.execute(text(f'DROP TABLE IF EXISTS "{row.partition_name}"'))
