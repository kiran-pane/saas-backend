import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.core.security.password import hash_password
from app.models.tenant import Tenant
from app.models.user import User
from app.services.rbac_service import assign_role, seed_default_roles


async def create_tenant(
    db: AsyncSession,
    slug: str,
    name: str,
    plan: str = "free",
    owner_email: str | None = None,
    owner_password: str | None = None,
) -> Tenant:
    """Creates a tenant with its default RBAC roles seeded. If
    owner_email/owner_password are given, also creates a first user and
    assigns it the tenant-wide Owner role atomically in the same
    transaction — this is the only supported way to bootstrap a tenant
    that isn't immediately locked out of its own RBAC console (see
    seed_default_roles for why Owner needs the full permission set)."""
    from sqlalchemy import select

    existing = await db.execute(select(Tenant).where(Tenant.slug == slug))
    if existing.scalar_one_or_none():
        raise ConflictError("Tenant slug already taken")

    tenant = Tenant(slug=slug, name=name, plan=plan)
    db.add(tenant)
    await db.flush()

    owner_role = await seed_default_roles(db, tenant.id)

    if owner_email and owner_password:
        owner_user = User(
            tenant_id=tenant.id,
            email=owner_email,
            hashed_password=hash_password(owner_password),
            full_name="Owner",
        )
        db.add(owner_user)
        await db.flush()
        await assign_role(
            db, user_id=owner_user.id, role_id=owner_role.id, org_unit_id=None,
            granted_by=owner_user.id, tenant_id=tenant.id,
        )

    # Tenant creation happens on the unauthenticated, cross-tenant
    # /platform/tenants route, which is deliberately exempt from
    # TenantMiddleware — there is no tenant in the request's ContextVar
    # to key off, so audit_service.record() (which requires one) can't be
    # used here. Enqueue directly instead, with the newly created
    # tenant's own id, so this event isn't silently unaudited — creating
    # a tenant is exactly the kind of event that shouldn't go unlogged
    # given this route currently has no auth gate.
    from app.tasks.audit_tasks import write_audit_log
    write_audit_log.delay(
        tenant_id=str(tenant.id), actor_id=None, action="tenant.created",
        resource_type="tenant", resource_id=str(tenant.id),
        metadata={"slug": slug, "plan": plan, "owner_bootstrapped": bool(owner_email)},
    )

    return tenant
