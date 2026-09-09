import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    # NOTE: in production this table is RANGE-partitioned by created_at
    # (monthly). See alembic/versions/0001_initial.py for the raw DDL,
    # since SQLAlchemy's ORM layer doesn't express partitioning directly.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None]
    actor_type: Mapped[str] = mapped_column(String(20), default="user")  # user|system|api_key
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # "user.role.granted"
    resource_type: Mapped[str | None] = mapped_column(String(100), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(100), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    audit_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
