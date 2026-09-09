"""Stripe provider. Uses Stripe Checkout (hosted page) for the purchase
flow — card data never touches this backend, keeping PCI scope at
SAQ-A."""
import asyncio

from app.payments.base import (
    EVENT_PAYMENT_FAILED,
    EVENT_PAYMENT_SUCCEEDED,
    EVENT_REFUND_ISSUED,
    EVENT_SUBSCRIPTION_CANCELED,
    EVENT_SUBSCRIPTION_UPDATED,
    EVENT_UNKNOWN,
    CheckoutSession,
    PaymentError,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
    WebhookVerificationError,
)

_STATUS_MAP = {
    "succeeded": "succeeded", "processing": "pending", "requires_payment_method": "failed",
    "canceled": "failed", "requires_action": "pending",
}

_EVENT_TYPE_MAP = {
    "checkout.session.completed": EVENT_PAYMENT_SUCCEEDED,
    "invoice.paid": EVENT_PAYMENT_SUCCEEDED,
    "invoice.payment_failed": EVENT_PAYMENT_FAILED,
    "customer.subscription.updated": EVENT_SUBSCRIPTION_UPDATED,
    "customer.subscription.deleted": EVENT_SUBSCRIPTION_CANCELED,
    "charge.refunded": EVENT_REFUND_ISSUED,
}


class StripePaymentProvider(PaymentProvider):
    def __init__(self, secret_key: str, webhook_secret: str):
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret

    def _client(self):
        import stripe
        stripe.api_key = self._secret_key
        return stripe

    async def create_checkout_session(self, tenant_id, plan_code, amount_cents, currency,
                                       success_url, cancel_url, customer_email=None) -> CheckoutSession:
        stripe = self._client()
        try:
            session = await asyncio.to_thread(
                stripe.checkout.Session.create,
                mode="subscription" if amount_cents > 0 else "payment",
                line_items=[{
                    "price_data": {
                        "currency": currency,
                        "product_data": {"name": plan_code},
                        "unit_amount": amount_cents,
                        **({"recurring": {"interval": "month"}} if amount_cents > 0 else {}),
                    },
                    "quantity": 1,
                }],
                success_url=success_url, cancel_url=cancel_url,
                customer_email=customer_email,
                metadata={"tenant_id": tenant_id, "plan_code": plan_code},
            )
        except stripe.error.StripeError as e:
            raise PaymentError(str(e)) from e
        return CheckoutSession(provider="stripe", checkout_url=session.url, provider_session_id=session.id)

    async def verify_webhook(self, payload: bytes, signature_header: str) -> WebhookEvent:
        stripe = self._client()
        try:
            event = await asyncio.to_thread(
                stripe.Webhook.construct_event, payload, signature_header, self._webhook_secret,
            )
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            raise WebhookVerificationError(str(e)) from e

        obj = event["data"]["object"]
        normalized_type = _EVENT_TYPE_MAP.get(event["type"], EVENT_UNKNOWN)
        metadata = obj.get("metadata") or {}
        return WebhookEvent(
            provider="stripe", event_id=event["id"], event_type=normalized_type, raw_payload=event,
            provider_subscription_id=obj.get("subscription") or (obj.get("id") if "subscription" in event["type"] else None),
            provider_payment_id=obj.get("payment_intent") or obj.get("id"),
            provider_customer_id=obj.get("customer"),
            amount_cents=obj.get("amount_total") or obj.get("amount_paid") or obj.get("amount"),
            currency=obj.get("currency"),
            status=_STATUS_MAP.get(obj.get("status", ""), None),
            tenant_id_hint=metadata.get("tenant_id"),
            plan_code_hint=metadata.get("plan_code"),
        )

    async def refund(self, provider_payment_id, amount_cents=None) -> RefundResult:
        stripe = self._client()
        try:
            kwargs = {"payment_intent": provider_payment_id}
            if amount_cents is not None:
                kwargs["amount"] = amount_cents
            refund = await asyncio.to_thread(stripe.Refund.create, **kwargs)
        except stripe.error.StripeError as e:
            raise PaymentError(str(e)) from e
        return RefundResult(provider_refund_id=refund.id, amount_cents=refund.amount, status=refund.status)

    async def get_payment_status(self, provider_payment_id: str) -> str:
        stripe = self._client()
        try:
            intent = await asyncio.to_thread(stripe.PaymentIntent.retrieve, provider_payment_id)
        except stripe.error.StripeError as e:
            raise PaymentError(str(e)) from e
        return _STATUS_MAP.get(intent.status, "pending")

    async def cancel_subscription(self, provider_subscription_id, at_period_end=True) -> None:
        stripe = self._client()
        try:
            if at_period_end:
                await asyncio.to_thread(stripe.Subscription.modify, provider_subscription_id,
                                         cancel_at_period_end=True)
            else:
                await asyncio.to_thread(stripe.Subscription.delete, provider_subscription_id)
        except stripe.error.StripeError as e:
            raise PaymentError(str(e)) from e
