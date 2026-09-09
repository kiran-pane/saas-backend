import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TenantScopedMixin, TimestampMixin, new_uuid


class KnowledgeBase(Base, TimestampMixin, TenantScopedMixin):
    """See CHAT_RAG_AGENT_ARCHITECTURE_V2.md section 1 for the full
    lifecycle rationale (access_mode default-open, embedding_model
    pinning, status transitions)."""
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    access_mode: Mapped[str] = mapped_column(String(20), default="tenant_wide")  # tenant_wide | restricted
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived | deleting
    document_count: Mapped[int] = mapped_column(default=0)
    chunk_count: Mapped[int] = mapped_column(default=0)
    total_size_bytes: Mapped[int] = mapped_column(default=0)
    embedding_model_deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class KnowledgeBaseAccessGrant(Base, TimestampMixin):
    """Only ever consulted when a KB's access_mode='restricted' — for the
    default tenant_wide case, this table isn't even queried. See
    architecture doc section 1, A.2 in the original plan."""
    __tablename__ = "knowledge_base_access_grants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    grantee_type: Mapped[str] = mapped_column(String(10), nullable=False)  # user | role
    grantee_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class UserChatPreference(Base, TimestampMixin):
    """One row per user — resolves architecture doc section 2's Problem 1
    (what KBs does a brand-new conversation start with)."""
    __tablename__ = "user_chat_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    default_kb_ids: Mapped[list] = mapped_column(JSON, default=list)


class UserMemoryFact(Base, TimestampMixin, TenantScopedMixin):
    """Long-lived, cross-conversation facts about a user. User-editable/
    deletable via /api/v1/memory — never an opaque, unremovable store.
    See architecture doc section 1, A.5."""
    __tablename__ = "user_memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fact_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    # "explicit_tool" (model called remember_this mid-conversation) |
    # "user_added" (user typed it directly into /memory settings)
    source: Mapped[str] = mapped_column(String(20), default="explicit_tool")
