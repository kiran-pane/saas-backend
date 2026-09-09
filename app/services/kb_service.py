"""Knowledge Base management + access resolution.

Access model (architecture doc section 1, A.2): default is tenant-wide —
every user sees every KB with no grants table lookup at all. Only when a
KB is flipped to access_mode='restricted' does
knowledge_base_access_grants get consulted, keeping the common (default)
path cheap.
"""
import uuid

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache.redis_client import get_redis
from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.models.knowledge import KnowledgeBase, KnowledgeBaseAccessGrant
from app.models.rbac import UserRole
from app.services.audit_service import record as audit_record

_KB_ACCESS_VERSION_KEY = "kb_access:version:{tenant_id}"
_KB_ACCESS_CACHE_KEY = "kb_access:{tenant_id}:{user_id}:v{version}"
_KB_ACCESS_TTL_SECONDS = 300


async def _get_kb_access_version(tenant_id: uuid.UUID) -> int:
    redis = await get_redis()
    key = _KB_ACCESS_VERSION_KEY.format(tenant_id=tenant_id)
    version = await redis.get(key)
    if version is None:
        await redis.set(key, 1)
        return 1
    return int(version)


async def bump_kb_access_version(tenant_id: uuid.UUID) -> None:
    """Call after any KB access_mode change or grant add/remove — same
    versioned-cache-invalidation pattern already used for RBAC
    permissions (app.core.security.permissions)."""
    redis = await get_redis()
    await redis.incr(_KB_ACCESS_VERSION_KEY.format(tenant_id=tenant_id))


async def get_accessible_kb_ids(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID,
                                 is_superadmin: bool = False) -> set[uuid.UUID]:
    """The single enforcement point referenced throughout the
    architecture doc — called both to populate the KB-toggle picker and,
    critically, inside the retrieval step itself (never trust a
    client-supplied KB id list without intersecting it against this)."""
    if is_superadmin:
        result = await db.execute(select(KnowledgeBase.id).where(KnowledgeBase.tenant_id == tenant_id))
        return {row[0] for row in result}

    redis = await get_redis()
    version = await _get_kb_access_version(tenant_id)
    cache_key = _KB_ACCESS_CACHE_KEY.format(tenant_id=tenant_id, user_id=user_id, version=version)
    cached = await redis.get(cache_key)
    if cached is not None:
        return {uuid.UUID(x) for x in orjson.loads(cached)}

    # tenant_wide KBs: everyone gets them, no grant needed.
    tenant_wide = await db.execute(
        select(KnowledgeBase.id).where(KnowledgeBase.tenant_id == tenant_id,
                                        KnowledgeBase.access_mode == "tenant_wide",
                                        KnowledgeBase.status != "deleting")
    )
    accessible = {row[0] for row in tenant_wide}

    # restricted KBs: only via an explicit user or role grant.
    user_role_ids = await db.execute(select(UserRole.role_id).where(UserRole.user_id == user_id))
    role_ids = [row[0] for row in user_role_ids]

    grants_query = select(KnowledgeBaseAccessGrant.knowledge_base_id).where(
        KnowledgeBaseAccessGrant.grantee_type == "user", KnowledgeBaseAccessGrant.grantee_id == user_id
    )
    grants = await db.execute(grants_query)
    accessible |= {row[0] for row in grants}

    if role_ids:
        role_grants = await db.execute(
            select(KnowledgeBaseAccessGrant.knowledge_base_id).where(
                KnowledgeBaseAccessGrant.grantee_type == "role",
                KnowledgeBaseAccessGrant.grantee_id.in_(role_ids),
            )
        )
        accessible |= {row[0] for row in role_grants}

    await redis.setex(cache_key, _KB_ACCESS_TTL_SECONDS, orjson.dumps([str(x) for x in accessible]))
    return accessible


async def create_knowledge_base(db: AsyncSession, tenant_id: uuid.UUID, created_by: uuid.UUID, name: str,
                                 description: str | None, embedding_model: str,
                                 embedding_dimension: int) -> KnowledgeBase:
    # CURRENT LIMITATION, deliberately enforced rather than silently
    # broken: document_chunks.embedding is a single pgvector column with
    # a FIXED dimension (vector(1536), set in migration 0001) — pgvector
    # does not support variable-dimension vectors in one column. Every KB
    # must therefore use an embedding model producing exactly 1536
    # dimensions (e.g. OpenAI's text-embedding-3-small) until a future
    # migration adds either a second fixed-dimension column for a
    # different model family, or partitions document_chunks by dimension
    # group. Not a silent constraint — surfaced here as a clear error.
    if embedding_dimension != 1536:
        raise ValidationAppError(
            "Only 1536-dimension embedding models are currently supported "
            "(document_chunks.embedding is a fixed-dimension pgvector column). "
            "Use an embedding model such as text-embedding-3-small."
        )

    existing = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == name)
    )
    if existing.scalar_one_or_none():
        raise ConflictError("A knowledge base with this name already exists")

    kb = KnowledgeBase(
        tenant_id=tenant_id, name=name, description=description, embedding_model=embedding_model,
        embedding_dimension=embedding_dimension, created_by=created_by,
    )
    db.add(kb)
    await db.flush()
    audit_record("kb.created", resource_type="knowledge_base", resource_id=str(kb.id),
                 metadata={"name": name, "embedding_model": embedding_model})
    return kb


async def set_kb_access_mode(db: AsyncSession, tenant_id: uuid.UUID, kb_id: uuid.UUID, access_mode: str,
                              actor_user_id: uuid.UUID) -> KnowledgeBase:
    if access_mode not in ("tenant_wide", "restricted"):
        raise ValidationAppError("access_mode must be 'tenant_wide' or 'restricted'")
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        raise NotFoundError("Knowledge base not found")
    kb.access_mode = access_mode
    await db.flush()
    await bump_kb_access_version(tenant_id)
    audit_record("kb.access_mode_changed", resource_type="knowledge_base", resource_id=str(kb_id),
                 actor_user_id=actor_user_id, metadata={"access_mode": access_mode})
    return kb


async def add_access_grant(db: AsyncSession, tenant_id: uuid.UUID, kb_id: uuid.UUID, grantee_type: str,
                            grantee_id: uuid.UUID, granted_by: uuid.UUID) -> KnowledgeBaseAccessGrant:
    if grantee_type not in ("user", "role"):
        raise ValidationAppError("grantee_type must be 'user' or 'role'")
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        raise NotFoundError("Knowledge base not found")

    grant = KnowledgeBaseAccessGrant(
        knowledge_base_id=kb_id, grantee_type=grantee_type, grantee_id=grantee_id, granted_by=granted_by,
    )
    db.add(grant)
    await db.flush()
    await bump_kb_access_version(tenant_id)
    audit_record("kb.access_granted", resource_type="knowledge_base", resource_id=str(kb_id),
                 actor_user_id=granted_by, metadata={"grantee_type": grantee_type, "grantee_id": str(grantee_id)})
    return grant


async def remove_access_grant(db: AsyncSession, tenant_id: uuid.UUID, grant_id: uuid.UUID,
                               actor_user_id: uuid.UUID) -> None:
    grant = await db.get(KnowledgeBaseAccessGrant, grant_id)
    if grant is None:
        raise NotFoundError("Access grant not found")
    kb = await db.get(KnowledgeBase, grant.knowledge_base_id)
    if kb is None or kb.tenant_id != tenant_id:
        raise NotFoundError("Access grant not found")
    await db.delete(grant)
    await db.flush()
    await bump_kb_access_version(tenant_id)
    audit_record("kb.access_revoked", resource_type="knowledge_base", resource_id=str(grant.knowledge_base_id),
                 actor_user_id=actor_user_id)


async def list_accessible_knowledge_bases(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID,
                                           is_superadmin: bool = False) -> list[KnowledgeBase]:
    accessible_ids = await get_accessible_kb_ids(db, tenant_id, user_id, is_superadmin)
    if not accessible_ids:
        return []
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id.in_(accessible_ids), KnowledgeBase.status != "deleting")
    )
    return list(result.scalars().all())
