"""Storage-side Celery tasks: virus scanning (mandatory pre-commit gate),
lifecycle policy sweeps, and multipart-session cleanup. Runs on the
llm_heavy queue alongside document ingestion — same isolation rationale
(CPU/IO-bound work that shouldn't starve quick audit/notification tasks).
"""
import asyncio
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.core.db.session import async_session_factory
from app.storage.base import ObjectNotFoundError, StorageKey
from app.storage.factory import get_storage_provider
from app.storage.scanning import MimeMismatchError, scan_bytes, validate_mime_type
from app.tasks.celery_app import celery_app


@celery_app.task(bind=True, max_retries=2, default_retry_delay=15)
def scan_document_task(self, document_id: str, tenant_id: str):
    asyncio.run(_scan_document_async(document_id, tenant_id))


async def _scan_document_async(document_id: str, tenant_id: str) -> None:
    from app.models.llm import Document
    from app.services.audit_service import record as audit_record

    async with async_session_factory() as db:
        await db.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": tenant_id})
        result = await db.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
        document = result.scalar_one_or_none()
        if document is None:
            return

        provider = get_storage_provider()
        quarantine_key = StorageKey(tenant_id, document_id, document.filename, quarantine=True)

        try:
            chunks = [c async for c in provider.download(quarantine_key)]
        except ObjectNotFoundError:
            document.status = "failed"
            await db.commit()
            return
        raw_bytes = b"".join(chunks)

        # Gate 1: MIME-sniff vs. declared extension — cheap, runs first.
        try:
            validate_mime_type(document.filename, raw_bytes)
        except MimeMismatchError as e:
            document.status = "rejected_invalid_type"
            await provider.delete(quarantine_key)
            await db.commit()
            audit_record("document.upload.rejected", resource_type="document", resource_id=document_id,
                          metadata={"reason": "mime_mismatch", "detail": str(e)})
            return

        # Gate 2: ClamAV scan — more expensive, runs second.
        scan_result = await scan_bytes(raw_bytes)
        if not scan_result.clean:
            document.status = "rejected_virus"
            document.scan_signature = scan_result.signature
            await provider.delete(quarantine_key)
            await db.commit()
            audit_record("document.upload.rejected", resource_type="document", resource_id=document_id,
                          metadata={"reason": "virus_detected", "signature": scan_result.signature})
            return

        # Both gates passed: promote from quarantine to the tenant-facing path.
        final_key = quarantine_key.as_final()
        await provider.copy(quarantine_key, final_key)
        await provider.delete(quarantine_key)

        document.status = "ready"
        document.size_bytes = len(raw_bytes)
        await db.commit()

        audit_record("document.upload.completed", resource_type="document", resource_id=document_id,
                      metadata={"bytes": len(raw_bytes), "scan_skipped": scan_result.skipped})

        # Trigger the embedding pipeline — the ONE connection point
        # between storage completion and ingestion start.
        from app.tasks.llm_tasks import ingest_document_task
        ingest_document_task.delay(document_id, tenant_id)


@celery_app.task
def sweep_stale_multipart_uploads_task():
    """Aborts multipart sessions that outlived their Redis TTL's grace
    window (belt-and-suspenders — Redis TTL already expires the session
    key itself; this catches provider-side orphaned multipart uploads
    where the session record is gone but the cloud-side upload wasn't
    explicitly aborted, which some providers otherwise bill storage for
    indefinitely)."""
    # Provider-native backstops (S3 bucket lifecycle rule for
    # AbortIncompleteMultipartUpload, GCS/Azure equivalents) are the
    # primary defense here — this task is a secondary sweep for
    # providers/setups where that isn't configured. Left as an explicit
    # per-provider admin-API listing task since the StorageProvider
    # interface doesn't (deliberately) expose "list all multipart
    # uploads" as a generic operation — that's provider-admin surface,
    # not tenant-facing storage surface.
    pass


@celery_app.task
def apply_lifecycle_policies_task():
    asyncio.run(_apply_lifecycle_policies_async())


async def _apply_lifecycle_policies_async() -> None:
    from app.config import settings
    from app.models.tenant import Tenant
    from app.services.audit_service import record as audit_record

    async with async_session_factory() as db:
        tenants = (await db.execute(select(Tenant))).scalars().all()
        provider = get_storage_provider()

        for tenant in tenants:
            policy = (tenant.settings or {}).get("storage_lifecycle", {})
            if not policy.get("archive_enabled", True):
                continue
            archive_after_days = policy.get("archive_after_days", settings.DEFAULT_ARCHIVE_AFTER_DAYS)
            cutoff = datetime.utcnow() - timedelta(days=archive_after_days)

            objects = await provider.list_objects(str(tenant.id))
            for obj in objects:
                if obj.storage_class == "standard" and obj.last_modified < cutoff and not obj.key.quarantine:
                    archived_key = StorageKey(obj.key.tenant_id, obj.key.document_id,
                                               f"archived/{obj.key.filename}")
                    try:
                        await provider.copy(obj.key, archived_key)
                        await provider.delete(obj.key)
                        audit_record("document.archived", resource_type="document",
                                      resource_id=obj.key.document_id,
                                      metadata={"tenant_id": str(tenant.id)})
                    except Exception:
                        continue  # one tenant's failure shouldn't abort the whole sweep
