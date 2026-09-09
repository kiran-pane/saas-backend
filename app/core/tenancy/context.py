import uuid
from contextvars import ContextVar

_current_tenant: ContextVar[uuid.UUID | None] = ContextVar("current_tenant", default=None)
_current_user: ContextVar[uuid.UUID | None] = ContextVar("current_user", default=None)


def set_tenant(tenant_id: uuid.UUID) -> None:
    _current_tenant.set(tenant_id)


def get_tenant() -> uuid.UUID:
    tid = _current_tenant.get()
    if tid is None:
        raise RuntimeError("No tenant in context. Did TenantMiddleware run for this request?")
    return tid


def get_tenant_optional() -> uuid.UUID | None:
    return _current_tenant.get()


def set_user(user_id: uuid.UUID) -> None:
    _current_user.set(user_id)


def get_user_optional() -> uuid.UUID | None:
    return _current_user.get()
