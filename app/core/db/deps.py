from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.session import async_session_factory
from app.core.tenancy.context import get_tenant_optional


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yields a request-scoped session with the Postgres RLS session
    variable set to the current tenant. This is what turns tenant
    isolation from an "we remembered to filter" convention into a
    database-enforced guarantee.

    IMPORTANT under PgBouncer transaction-mode pooling: `SET` is
    connection-scoped, and PgBouncer may hand the underlying connection
    to a different logical session between transactions. We therefore
    re-issue the SET on every checkout (i.e. every request), never rely
    on it persisting.
    """
    async with async_session_factory() as session:
        tenant_id = get_tenant_optional()
        if tenant_id is not None:
            await session.execute(
                text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)}
            )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db_no_tenant() -> AsyncIterator[AsyncSession]:
    """For platform/superadmin operations that legitimately span tenants
    (e.g. creating a new tenant). Never expose this on tenant-facing routes."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_system_db() -> AsyncIterator[AsyncSession]:
    """For continuous, genuinely cross-tenant background workloads (the
    live-stream supervisor) — NOT for API routes, and NOT a substitute
    for get_db_no_tenant(). See app/core/db/session.py::system_engine for
    the full rationale and production hardening requirements."""
    from app.core.db.session import system_session_factory

    async with system_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
