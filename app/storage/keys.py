"""The only sanctioned way to build a StorageKey from a Document row.
Never construct StorageKey by hand from a route/service — always go
through build_storage_key(), which asserts the document actually belongs
to the current tenant context before a key can even be created."""
from app.core.tenancy.context import get_tenant
from app.models.llm import Document
from app.storage.base import InvalidStorageKeyError, StorageKey


def build_storage_key(document: Document, quarantine: bool = False) -> StorageKey:
    current_tenant = get_tenant()
    if str(document.tenant_id) != str(current_tenant):
        # A Document row from another tenant must never even reach the
        # point of building a StorageKey — same defense-in-depth principle
        # as the Postgres RLS policy on the documents table itself.
        raise InvalidStorageKeyError("Document does not belong to the current tenant context")
    return StorageKey(
        tenant_id=str(document.tenant_id),
        document_id=str(document.id),
        filename=document.filename,
        quarantine=quarantine,
    )


def build_storage_key_for_task(document: Document, tenant_id: str, quarantine: bool = False) -> StorageKey:
    """Celery-task variant: there is no request-scoped tenant ContextVar
    inside a worker process, so the tenant_id is validated against the
    value explicitly passed into the task (itself sourced from the
    Document row at enqueue time — see app/tasks/llm_tasks.py) instead of
    a ContextVar. Still fails closed on any mismatch."""
    if str(document.tenant_id) != str(tenant_id):
        raise InvalidStorageKeyError("Document tenant_id does not match the task's tenant_id argument")
    return StorageKey(
        tenant_id=str(document.tenant_id),
        document_id=str(document.id),
        filename=document.filename,
        quarantine=quarantine,
    )
