import asyncio

from app.tasks.celery_app import celery_app


@celery_app.task(bind=True, max_retries=2, default_retry_delay=15)
def ingest_document_task(self, document_id: str, tenant_id: str):
    """Runs on the llm_heavy queue (separate worker pool, lower
    concurrency) so a burst of ingestion jobs never starves quick audit/
    notification tasks on the shared queues. Triggered automatically by
    app.tasks.storage_tasks.scan_document_task once a document passes
    virus scanning and MIME validation and is committed to its final
    storage location."""
    asyncio.run(_ingest_document_async(document_id, tenant_id))


async def _ingest_document_async(document_id: str, tenant_id: str):
    import uuid

    from sqlalchemy import select, text

    from app.core.db.session import async_session_factory
    from app.llm.ingestion.pipeline import ingest_document
    from app.models.llm import Document

    async with async_session_factory() as db:
        await db.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": tenant_id})
        result = await db.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
        document = result.scalar_one_or_none()
        if document is None:
            return
        if document.status != "ready":
            # Defensive: only ingest documents that actually passed the
            # storage+scan gate. A document in any other state (still
            # scanning, rejected, already indexed) should never reach
            # chunking/embedding via this path.
            return

        try:
            # raw_content is intentionally omitted here — ingest_document
            # fetches it from the storage layer itself via
            # fetch_document_bytes(), keeping this task's signature
            # (document_id, tenant_id) stable regardless of which
            # STORAGE_PROVIDER is configured.
            await ingest_document(db, document)
            await db.commit()
        except Exception:
            document.status = "failed"
            await db.commit()
            raise
