import uuid

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base, TenantScopedMixin, TimestampMixin, new_uuid


class User(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nullable: OAuth-only accounts (e.g. Google sign-in) never set a
    # local password. auth_service enforces "has a usable auth method"
    # at the point of login/registration, not via a NOT NULL constraint.
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organization_units.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)

    # OAuth identity, e.g. provider="google", oauth_sub=Google's stable
    # per-user "sub" claim (never the email — emails can be reassigned).
    oauth_provider: Mapped[str | None] = mapped_column(String(30))
    oauth_sub: Mapped[str | None] = mapped_column(String(255))

    roles: Mapped[list["UserRole"]] = relationship(back_populates="user", lazy="noload")  # noqa: F821


class RefreshToken(Base, TimestampMixin):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
