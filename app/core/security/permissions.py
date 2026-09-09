import uuid

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache.redis_client import get_redis

# Recursive walk: user -> user_roles -> roles (+ parent chain via
# parent_role_id) -> role_permissions -> permissions. Cached per
# (tenant, user, rbac_version) so cache invalidation is O(1) — bump the
# version counter instead of scanning/deleting individual user keys.

_RBAC_VERSION_KEY = "rbac:version:{tenant_id}"
_PERMS_CACHE_KEY = "perms:{tenant_id}:{user_id}:v{version}"
_PERMS_TTL_SECONDS = 300

_EFFECTIVE_PERMISSIONS_SQL = text(
    """
    WITH RECURSIVE role_chain AS (
        SELECT r.id
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = :user_id
        UNION
        SELECT r.parent_role_id
        FROM roles r
        JOIN role_chain rc ON rc.id = r.id
        WHERE r.parent_role_id IS NOT NULL
    )
    SELECT DISTINCT p.code
    FROM permissions p
    JOIN role_permissions rp ON rp.permission_id = p.id
    JOIN role_chain rc ON rc.id = rp.role_id
    """
)


async def get_rbac_version(tenant_id: uuid.UUID) -> int:
    redis = await get_redis()
    key = _RBAC_VERSION_KEY.format(tenant_id=tenant_id)
    version = await redis.get(key)
    if version is None:
        await redis.set(key, 1)
        return 1
    return int(version)


async def bump_rbac_version(tenant_id: uuid.UUID) -> None:
    redis = await get_redis()
    await redis.incr(_RBAC_VERSION_KEY.format(tenant_id=tenant_id))


async def get_effective_permissions(
    user_id: uuid.UUID, tenant_id: uuid.UUID, db: AsyncSession
) -> set[str]:
    redis = await get_redis()
    version = await get_rbac_version(tenant_id)
    cache_key = _PERMS_CACHE_KEY.format(tenant_id=tenant_id, user_id=user_id, version=version)

    cached = await redis.get(cache_key)
    if cached is not None:
        return set(orjson.loads(cached))

    rows = await db.execute(_EFFECTIVE_PERMISSIONS_SQL, {"user_id": str(user_id)})
    permissions = {row[0] for row in rows}

    await redis.setex(cache_key, _PERMS_TTL_SECONDS, orjson.dumps(list(permissions)))
    return permissions
