import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.core.security.permissions import bump_rbac_version
from app.models.rbac import Role, RolePermission, UserRole
from app.models.user import User
from app.repositories.rbac_repo import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)


async def create_role(db: AsyncSession, tenant_id: uuid.UUID, name: str,
                       parent_role_id: uuid.UUID | None) -> Role:
    repo = RoleRepository(db)
    if await repo.get_by_name(tenant_id, name):
        raise ConflictError("A role with this name already exists")

    if parent_role_id is not None:
        parent = await repo.get_by_id(parent_role_id)
        if parent is None or (parent.tenant_id not in (None, tenant_id)):
            raise NotFoundError("Parent role not found")

    role = Role(tenant_id=tenant_id, name=name, parent_role_id=parent_role_id)
    role = await repo.add(role)
    return role


async def set_role_parent(db: AsyncSession, role_id: uuid.UUID, new_parent_id: uuid.UUID,
                           tenant_id: uuid.UUID, actor_user_id: uuid.UUID) -> Role:
    from app.services.audit_service import record as audit_record

    repo = RoleRepository(db)
    role = await repo.get_by_id(role_id, tenant_id)
    if role is None:
        raise NotFoundError("Role not found")

    if await repo.would_create_cycle(role_id, new_parent_id):
        raise ValidationAppError("This would create a cycle in the role hierarchy")

    old_parent_id = role.parent_role_id
    role.parent_role_id = new_parent_id
    await db.flush()
    await bump_rbac_version(tenant_id)
    audit_record("role.reparented", resource_type="role", resource_id=str(role_id),
                 actor_user_id=actor_user_id,
                 metadata={"old_parent_id": str(old_parent_id) if old_parent_id else None,
                           "new_parent_id": str(new_parent_id)})
    return role


async def attach_permission(db: AsyncSession, role_id: uuid.UUID, permission_code: str,
                             tenant_id: uuid.UUID) -> None:
    role_repo = RoleRepository(db)
    role = await role_repo.get_by_id(role_id)
    if role is None or role.tenant_id not in (None, tenant_id):
        raise NotFoundError("Role not found")

    perm = await PermissionRepository(db).get_by_code(permission_code)
    if perm is None:
        raise NotFoundError(f"Unknown permission code: {permission_code}")

    await RolePermissionRepository(db).add(RolePermission(role_id=role_id, permission_id=perm.id))
    await bump_rbac_version(tenant_id)


async def assign_role(db: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID,
                       org_unit_id: uuid.UUID | None, granted_by: uuid.UUID,
                       tenant_id: uuid.UUID) -> UserRole:
    user_role = UserRole(
        user_id=user_id, role_id=role_id, org_unit_id=org_unit_id, granted_by=granted_by
    )
    user_role = await UserRoleRepository(db).add(user_role)
    await bump_rbac_version(tenant_id)
    return user_role


async def list_roles(db: AsyncSession, tenant_id: uuid.UUID, limit: int = 50) -> list[Role]:
    return await RoleRepository(db).list_for_tenant(tenant_id, limit=limit)


async def seed_default_roles(db: AsyncSession, tenant_id: uuid.UUID) -> Role:
    """Called once at tenant creation. "Owner" and "Member" are system
    roles seeded per-tenant so every tenant starts with a sane baseline
    without an admin having to build RBAC from scratch on day one.

    Critically, "Owner" is granted every permission that currently exists
    in the `permissions` table (seeded by the 0001 migration and kept in
    sync by app/seeds/permissions.py) — without this, a freshly created
    tenant would have no way to bootstrap RBAC management at all, since
    the seeded Owner role would otherwise carry zero permissions. "Member"
    gets the low-privilege read-only subset defined in
    DEFAULT_MEMBER_PERMISSION_CODES. Returns the Owner role so callers
    (e.g. the dev-data seed script) can assign it to a bootstrap user.
    """
    from sqlalchemy import select

    from app.core.security.permission_catalog import DEFAULT_MEMBER_PERMISSION_CODES
    from app.models.rbac import Permission

    repo = RoleRepository(db)
    owner = await repo.add(Role(tenant_id=tenant_id, name="Owner", is_system=True))
    member = await repo.add(Role(tenant_id=tenant_id, name="Member", is_system=True))

    all_permissions = (await db.execute(select(Permission))).scalars().all()

    role_perm_repo = RolePermissionRepository(db)
    for perm in all_permissions:
        await role_perm_repo.add(RolePermission(role_id=owner.id, permission_id=perm.id))
        if perm.code in DEFAULT_MEMBER_PERMISSION_CODES:
            await role_perm_repo.add(RolePermission(role_id=member.id, permission_id=perm.id))

    return owner
