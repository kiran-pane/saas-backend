"""Provider-agnostic payment interface — same abstraction pattern as
app.storage.base.StorageProvider: a unified interface, thin provider
adapters, exceptions translated into a shared hierarchy, and a factory
that supports per-region routing (see factory.py) rather than a single
global provider choice, since real deployments commonly route Razorpay
for India/UPI-heavy tenants, Stripe for US/EU, PayPal as an alternative
checkout option — not "pick exactly one gateway forever."
"""
import abc
from dataclasses import dataclass


class PaymentError(Exception):
    """Base class — callers catch this, never a provider SDK's native exception."""


class WebhookVerificationError(PaymentError):
    """Raised when a webhook's signature fails verification. The payload
    MUST NOT be trusted or processed if this is raised — never
    parse-then-verify."""


class PaymentNotFoundError(PaymentError):
    pass


@dataclass
class CheckoutSession:
    provider: str
    checkout_url: str
    provider_session_id: str
    # extra_fields covers providers (Razorpay) whose "checkout" is a
    # client-side SDK invocation rather than a redirect URL — in that
    # case checkout_url may be empty and the frontend uses extra_fields
    # (order_id, key_id, amount) to open the provider's own JS checkout.
    extra_fields: dict | None = None


@dataclass
class WebhookEvent:
    provider: str
    event_id: str
    event_type: str  # normalized, see _normalize_event_type per provider
    raw_payload: dict
    # Fields populated for subscription/payment-relevant event types;
    # None for event types that don't carry them.
    provider_subscription_id: str | None = None
    provider_payment_id: str | None = None
    provider_customer_id: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    status: str | None = None  # normalized subscription/payment status
    # Populated only for events that echo back the checkout-time metadata
    # (tenant_id embedded when the CheckoutSession was created) — e.g.
    # Stripe's checkout.session.completed, Razorpay's order "notes",
    # PayPal's custom_id. Renewal/update events on an already-linked
    # subscription won't have this; tenant resolution then falls back to
    # subscription_provider_index (see app/services/payment_service.py).
    tenant_id_hint: str | None = None
    plan_code_hint: str | None = None


@dataclass
class RefundResult:
    provider_refund_id: str
    amount_cents: int
    status: str


# Normalized status vocabulary every provider adapter must map onto —
# services/webhook consumers only ever see these values, never a
# provider-native status string.
NORMALIZED_SUBSCRIPTION_STATUSES = {"incomplete", "active", "past_due", "canceled", "unpaid"}
NORMALIZED_PAYMENT_STATUSES = {"pending", "succeeded", "failed", "refunded"}

# Normalized event-type vocabulary — providers use wildly different
# naming (Stripe: "invoice.paid"; Razorpay: "payment.captured"; PayPal:
# "PAYMENT.SALE.COMPLETED") for conceptually the same event.
EVENT_PAYMENT_SUCCEEDED = "payment.succeeded"
EVENT_PAYMENT_FAILED = "payment.failed"
EVENT_SUBSCRIPTION_UPDATED = "subscription.updated"
EVENT_SUBSCRIPTION_CANCELED = "subscription.canceled"
EVENT_REFUND_ISSUED = "refund.issued"
EVENT_UNKNOWN = "unknown"


class PaymentProvider(abc.ABC):
    @abc.abstractmethod
    async def create_checkout_session(
        self, tenant_id: str, plan_code: str, amount_cents: int, currency: str,
        success_url: str, cancel_url: str, customer_email: str | None = None,
    ) -> CheckoutSession:
        ...

    @abc.abstractmethod
    async def verify_webhook(self, payload: bytes, signature_header: str) -> WebhookEvent:
        """Signature verification MUST happen before the payload is
        parsed into a trusted WebhookEvent — never parse-then-verify."""
        ...

    @abc.abstractmethod
    async def refund(self, provider_payment_id: str, amount_cents: int | None = None) -> RefundResult:
        """amount_cents=None means a full refund."""
        ...

    @abc.abstractmethod
    async def get_payment_status(self, provider_payment_id: str) -> str:
        """Returns a normalized status from NORMALIZED_PAYMENT_STATUSES."""
        ...

    @abc.abstractmethod
    async def cancel_subscription(self, provider_subscription_id: str, at_period_end: bool = True) -> None:
        ...
