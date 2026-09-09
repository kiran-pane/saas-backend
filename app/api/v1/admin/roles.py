import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.db.deps import get_db
from app.core.ratelimit.limiter import rate_limit
from app.core.tenancy.context import get_tenant
from app.models.user import User
from app.schemas.rbac import AssignRole, AttachPermission, RoleCreate, RoleOut, SetRoleParent
from app.services import rbac_service
from app.services.audit_service import record as audit_record

router = APIRouter(prefix="/admin/roles", tags=["admin:rbac"])


@router.get("", response_model=list[RoleOut])
async def list_roles(
    user: User = Depends(require_permission("rbac.manage")),
    db: AsyncSession = Depends(get_db),
):
    return await rbac_service.list_roles(db, tenant_id=get_tenant())


@router.post("", response_model=RoleOut, status_code=201,
             dependencies=[Depends(rate_limit("admin.write", 20, 60))])
async def create_role(
    payload: RoleCreate,
    user: User = Depends(require_permission("rbac.manage")),
    db: AsyncSession = Depends(get_db),
):
    role = await rbac_service.create_role(db, get_tenant(), payload.name, payload.parent_role_id)
    audit_record("role.created", resource_type="role", resource_id=str(role.id),
                 metadata=payload.model_dump(mode="json"), actor_user_id=user.id)
    return role


@router.post("/{role_id}/permissions", status_code=200)
async def attach_permission(
    role_id: uuid.UUID,
    payload: AttachPermission,
    user: User = Depends(require_permission("rbac.manage")),
    db: AsyncSession = Depends(get_db),
):
    await rbac_service.attach_permission(db, role_id, payload.permission_code, get_tenant())
    audit_record("role.permission_attached", resource_type="role", resource_id=str(role_id),
                 metadata=payload.model_dump(mode="json"), actor_user_id=user.id)
    return {"status": "attached"}


@router.post("/assign", status_code=200)
async def assign_role(
    payload: AssignRole,
    user: User = Depends(require_permission("rbac.manage")),
    db: AsyncSession = Depends(get_db),
):
    await rbac_service.assign_role(
        db, payload.user_id, payload.role_id, payload.org_unit_id, user.id, get_tenant()
    )
    audit_record("role.assigned", resource_type="user", resource_id=str(payload.user_id),
                 metadata=payload.model_dump(mode="json"), actor_user_id=user.id)
    return {"status": "assigned"}


@router.post("/{role_id}/parent", response_model=RoleOut)
async def reparent_role(
    role_id: uuid.UUID,
    payload: SetRoleParent,
    user: User = Depends(require_permission("rbac.manage")),
    db: AsyncSession = Depends(get_db),
):
    # Audit logging for this mutation happens inside rbac_service.set_role_parent
    # itself (not here) since it needs the old parent_role_id, which is only
    # known at the point of mutation.
    return await rbac_service.set_role_parent(db, role_id, payload.new_parent_id, get_tenant(), user.id)
