import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TenantScopedMixin, TimestampMixin, new_uuid


class Plan(Base, TimestampMixin):
    """Global catalog, not tenant-scoped — every tenant chooses from the
    same set of plans. Prices are stored in the smallest currency unit
    (cents) to avoid floating-point rounding issues in billing math."""
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # "pro_monthly"
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    amount_cents: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    interval: Mapped[str] = mapped_column(String(20), default="month")  # month|year|one_time
    is_active: Mapped[bool] = mapped_column(default=True)


class Subscription(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"))
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # stripe|razorpay|paypal
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255))
    provider_customer_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="incomplete")
    # incomplete|active|past_due|canceled|unpaid — provider-normalized,
    # see app/payments/base.py::normalize_subscription_status
    current_period_end: Mapped[datetime | None] = mapped_column()
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False)


class Payment(Base, TimestampMixin, TenantScopedMixin):
    """One row per completed/attempted payment (subscription renewal or
    one-time purchase). Never stores card/bank details — only
    provider-issued references, consistent with PCI-DSS SAQ-A scope
    minimization (the provider's hosted checkout keeps card data
    entirely on their side)."""
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subscriptions.id"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_cents: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending|succeeded|failed|refunded
    failure_reason: Mapped[str | None] = mapped_column(String(500))


class PaymentWebhookEvent(Base, TimestampMixin):
    """Dedup table for inbound webhooks — every provider retries webhook
    delivery, so processing must be idempotent. Not tenant-scoped: a
    webhook arrives before we necessarily know which tenant it belongs to
    (that's resolved during processing from the event payload itself)."""
    __tablename__ = "payment_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_webhook_provider_event"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    processed: Mapped[bool] = mapped_column(default=False)
    processing_error: Mapped[str | None] = mapped_column(String(1000))


class SubscriptionProviderIndex(Base):
    """Deliberately NOT tenant-scoped / NOT RLS-protected — see migration
    0006 for the full rationale. Used only to resolve which tenant a
    webhook's provider_subscription_id belongs to, before the RLS session
    variable can be set for the real read/write against `subscriptions`."""
    __tablename__ = "subscription_provider_index"

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider_subscription_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
