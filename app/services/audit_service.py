import uuid

import structlog

from app.core.tenancy.context import get_tenant_optional, get_user_optional
from app.tasks.audit_tasks import write_audit_log


def record(action: str, resource_type: str | None = None, resource_id: str | None = None,
           metadata: dict | None = None, request_id: str | None = None,
           actor_user_id: uuid.UUID | None = None) -> None:
    """Fire-and-forget audit write. Enqueued via Celery (never written
    inline in the request path) so a slow/locked audit table never adds
    latency to a user-facing request — see blueprint section 6.

    request_id is pulled automatically from RequestContextMiddleware's
    structlog contextvars unless explicitly overridden — callers don't
    need to thread it through manually, and it stays correct even if
    record() is called several layers deep in a service."""
    tenant_id = get_tenant_optional()
    if tenant_id is None:
        return  # platform-level actions without tenant context are logged separately

    actor = actor_user_id or get_user_optional()
    if request_id is None:
        request_id = structlog.contextvars.get_contextvars().get("request_id")

    write_audit_log.delay(
        tenant_id=str(tenant_id),
        actor_id=str(actor) if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata or {},
        request_id=request_id,
    )
