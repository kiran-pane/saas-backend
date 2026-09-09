import uuid

from sqlalchemy import select

from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.repositories.base import BaseRepository


class RoleRepository(BaseRepository[Role]):
    model = Role

    async def get_by_name(self, tenant_id: uuid.UUID, name: str) -> Role | None:
        stmt = select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_tenant(self, tenant_id: uuid.UUID, limit: int = 50) -> list[Role]:
        stmt = select(Role).where(Role.tenant_id == tenant_id).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def would_create_cycle(self, role_id: uuid.UUID, new_parent_id: uuid.UUID) -> bool:
        """Walk up from new_parent_id; if we ever reach role_id, assigning
        new_parent_id as role_id's parent would create a cycle in the
        role-inheritance DAG."""
        current = new_parent_id
        visited = set()
        while current is not None:
            if current == role_id:
                return True
            if current in visited:
                break
            visited.add(current)
            role = await self.get_by_id(current)
            current = role.parent_role_id if role else None
        return False


class PermissionRepository(BaseRepository[Permission]):
    model = Permission

    async def get_by_code(self, code: str) -> Permission | None:
        stmt = select(Permission).where(Permission.code == code)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()


class UserRoleRepository(BaseRepository[UserRole]):
    model = UserRole


class RolePermissionRepository(BaseRepository[RolePermission]):
    model = RolePermission
