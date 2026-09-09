import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for every ORM model in the app."""
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TenantScopedMixin:
    """Every tenant-owned table gets this. Combined with a Postgres RLS
    policy on the same column, this is a defense-in-depth guarantee:
    even a buggy query that forgets to filter by tenant_id cannot leak
    another tenant's rows, because Postgres itself enforces it."""
    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
