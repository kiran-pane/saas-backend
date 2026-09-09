import uuid

from fastapi import Request
from sqlalchemy import select

from app.core.db.session import async_session_factory
from app.models.tenant import Tenant


async def resolve_tenant(request: Request) -> Tenant | None:
    """Resolution order:
    1. X-Tenant-Slug header (API clients, mobile apps)
    2. Subdomain of the Host header (acme.yourapp.com -> "acme")
    3. Already-validated JWT tenant claim (set by get_current_user on
       subsequent dependency resolution; auth routes rely on 1/2 only)
    """
    slug = request.headers.get("X-Tenant-Slug")

    if not slug:
        host = request.headers.get("host", "")
        parts = host.split(".")
        if len(parts) >= 3:  # acme.yourapp.com
            slug = parts[0]

    if not slug:
        return None

    async with async_session_factory() as session:
        result = await session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()


async def resolve_tenant_by_id(tenant_id: uuid.UUID) -> Tenant | None:
    async with async_session_factory() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        return result.scalar_one_or_none()
