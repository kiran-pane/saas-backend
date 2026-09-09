"""Idempotent permission-catalog sync. Safe to run repeatedly, in any
environment, at any time — inserts new permission codes, updates the
resource/action/description of existing ones if you've edited them in
permission_catalog.py, and never touches role_permissions (so existing
grants are untouched even if a permission's description changes)."""
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security.permission_catalog import PERMISSION_CATALOG
from app.models.rbac import Permission


async def sync_permission_catalog(db: AsyncSession) -> int:
    if not PERMISSION_CATALOG:
        return 0

    stmt = insert(Permission).values(
        [
            {"code": code, "resource": resource, "action": action, "description": description}
            for code, resource, action, description in PERMISSION_CATALOG
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Permission.code],
        set_={
            "resource": stmt.excluded.resource,
            "action": stmt.excluded.action,
            "description": stmt.excluded.description,
        },
    )
    await db.execute(stmt)
    await db.flush()
    return len(PERMISSION_CATALOG)
