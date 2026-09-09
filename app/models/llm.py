import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TenantScopedMixin, TimestampMixin, new_uuid


class Project(Base, TimestampMixin, TenantScopedMixin):
    """A durable container for related conversations (ChatGPT/Claude
    "Projects"). Optional — most conversations never belong to one. See
    CHAT_RAG_AGENT_ARCHITECTURE_V2.md section 2."""
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_instructions: Mapped[str | None] = mapped_column(Text)
    default_kb_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class Conversation(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"),
                                                          index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    # pending: not yet generated (fallback to first-message heuristic in the
    # meantime) | generated: async title job completed | fallback: heuristic used permanently
    title_generation_status: Mapped[str] = mapped_column(String(20), default="pending")
    forked_from_conversation_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    forked_at_message_id: Mapped[uuid.UUID | None] = mapped_column()
    # LangGraph checkpoints for this conversation's thread_id live in a
    # separate table managed by langgraph-checkpoint-postgres (see
    # app/llm/graphs/checkpointer.py) — this row plus Message (below) is
    # the app-facing metadata/history; the checkpoint is execution
    # plumbing for resuming a turn, not user-facing history.


class Message(Base, TenantScopedMixin):
    """Durable, queryable turn-by-turn history — distinct from LangGraph's
    checkpoint state (see architecture doc section 2 / A.5 for why both
    exist). This is what a chat history UI lists, searches, and exports."""
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column()
    # Which KBs were actually queried for this turn — answers "why did it
    # say that" without needing to inspect LangGraph checkpoint internals.
    used_kb_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class ConversationKnowledgeBase(Base):
    """The chat-time KB toggle — which KBs are enabled for RAG retrieval
    in a specific conversation. Presence of a row = enabled. Not a
    TimestampMixin table; this is a pure join/toggle table."""
    __tablename__ = "conversation_knowledge_bases"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )


class Document(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "documents"

    # Valid status values (application-enforced, no DB CHECK constraint):
    # pending_upload -> scanning -> ready | rejected_virus |
    # rejected_invalid_type | failed | archived | superseded
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"), index=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(50), default="markdown")  # markdown|transcript|prose|...
    status: Mapped[str] = mapped_column(String(20), default="pending_upload")
    source_uri: Mapped[str | None] = mapped_column(String(1000))
    storage_provider: Mapped[str] = mapped_column(String(20), default="local")
    size_bytes: Mapped[int | None] = mapped_column()
    content_type: Mapped[str | None] = mapped_column(String(150))
    scan_signature: Mapped[str | None] = mapped_column(String(255))
    # Document-update lifecycle (architecture doc section 1, Problem 3):
    # an update is a NEW Document row, old one marked status='superseded'
    # with this pointer — chunks are never edited/replaced in place.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(index=True)


class DocumentChunk(Base, TenantScopedMixin):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(index=True)  # denormalized from document
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(index=True)  # hierarchical retrieval
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # dedup check, see pipeline.py
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict)  # includes chunker_version
    # Pinned per-chunk (not just per-KB) so a KB can legitimately have
    # chunks in two embedding generations during a blue/green re-embed
    # migration — see architecture doc section 3, Problem 2.
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    # `embedding` column (pgvector `vector(N)`) is added via raw SQL in
    # the Alembic migration — pgvector's SQLAlchemy type requires the
    # extension to exist first, so it's created there rather than here.
