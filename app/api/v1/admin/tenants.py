from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr

from app.core.db.deps import get_db_no_tenant
from app.services import tenant_service

# Platform-level (cross-tenant) route — deliberately NOT behind
# TenantMiddleware's normal resolution (see EXEMPT_PATH_PREFIXES). In
# production, gate this behind platform-operator auth (separate from
# tenant-user auth), e.g. an internal-only network policy or a distinct
# superadmin JWT audience — left as an explicit integration point.
router = APIRouter(prefix="/platform/tenants", tags=["platform"])


class TenantCreateRequest(BaseModel):
    slug: str
    name: str
    plan: str = "free"
    # Optional: bootstraps a first Owner user in the same transaction so
    # the new tenant isn't immediately stuck with no way to log in or
    # manage its own RBAC. Omit both to create an empty tenant instead
    # (e.g. if your signup flow creates the user separately).
    owner_email: EmailStr | None = None
    owner_password: str | None = None


@router.post("", status_code=201)
async def create_tenant(payload: TenantCreateRequest, db=Depends(get_db_no_tenant)):
    tenant = await tenant_service.create_tenant(
        db, payload.slug, payload.name, payload.plan,
        owner_email=payload.owner_email, owner_password=payload.owner_password,
    )
    return {"id": str(tenant.id), "slug": tenant.slug}
