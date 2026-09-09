from sqlalchemy import text

from app.tasks.audit_tasks import _get_engine
from app.tasks.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def send_email(self, to: str, subject: str, body: str):
    # Wire to your transactional email provider (SES/SendGrid/Postmark).
    # Kept as an explicit integration point rather than a fake send.
    print(f"[email] to={to} subject={subject!r}")


@celery_app.task
def cleanup_expired_tokens():
    with _get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM refresh_tokens WHERE expires_at < now() - interval '7 days'")
        )
