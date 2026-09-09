"""Optional demo/dev seed data: one tenant with a bootstrap Owner user.

Idempotent — safe to run repeatedly. Controlled entirely by env vars
(see .env.example) so it never accidentally creates demo data with a
guessable password in an environment where you didn't intend it to; if
SEED_TENANT_SLUG is unset, this is a no-op.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import tenant_service


async def seed_dev_environment(db: AsyncSession) -> str | None:
    if not settings.SEED_TENANT_SLUG:
        return None

    from sqlalchemy import select

    from app.models.tenant import Tenant

    existing = await db.execute(select(Tenant).where(Tenant.slug == settings.SEED_TENANT_SLUG))
    if existing.scalar_one_or_none():
        return f"Tenant '{settings.SEED_TENANT_SLUG}' already exists — skipped"

    if not (settings.SEED_ADMIN_EMAIL and settings.SEED_ADMIN_PASSWORD):
        raise ValueError(
            "SEED_TENANT_SLUG is set but SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD are missing"
        )

    tenant = await tenant_service.create_tenant(
        db,
        slug=settings.SEED_TENANT_SLUG,
        name=settings.SEED_TENANT_NAME or settings.SEED_TENANT_SLUG,
        owner_email=settings.SEED_ADMIN_EMAIL,
        owner_password=settings.SEED_ADMIN_PASSWORD,
    )
    return (
        f"Created tenant '{tenant.slug}' with owner '{settings.SEED_ADMIN_EMAIL}' "
        f"— log in with X-Tenant-Slug: {tenant.slug}"
    )
