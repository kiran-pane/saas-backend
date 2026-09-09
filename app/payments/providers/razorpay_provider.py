"""Razorpay provider. Razorpay's "checkout" is a client-side JS SDK
invocation, not a hosted redirect page — create_checkout_session returns
an order_id + key_id via extra_fields for the frontend to open
Razorpay's Checkout.js with, rather than a checkout_url to redirect to."""
import asyncio
import hashlib
import hmac
import json

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

_EVENT_TYPE_MAP = {
    "payment.captured": EVENT_PAYMENT_SUCCEEDED,
    "payment.failed": EVENT_PAYMENT_FAILED,
    "subscription.activated": EVENT_SUBSCRIPTION_UPDATED,
    "subscription.charged": EVENT_SUBSCRIPTION_UPDATED,
    "subscription.cancelled": EVENT_SUBSCRIPTION_CANCELED,
    "refund.processed": EVENT_REFUND_ISSUED,
}


class RazorpayPaymentProvider(PaymentProvider):
    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret

    def _client(self):
        import razorpay
        return razorpay.Client(auth=(self._key_id, self._key_secret))

    async def create_checkout_session(self, tenant_id, plan_code, amount_cents, currency,
                                       success_url, cancel_url, customer_email=None) -> CheckoutSession:
        client = self._client()
        try:
            order = await asyncio.to_thread(
                client.order.create, {
                    "amount": amount_cents, "currency": currency.upper(),
                    "notes": {"tenant_id": tenant_id, "plan_code": plan_code},
                }
            )
        except Exception as e:  # razorpay.errors.BadRequestError subclasses Exception
            raise PaymentError(str(e)) from e
        return CheckoutSession(
            provider="razorpay", checkout_url="", provider_session_id=order["id"],
            extra_fields={"order_id": order["id"], "key_id": self._key_id, "amount": amount_cents,
                          "currency": currency.upper()},
        )

    async def verify_webhook(self, payload: bytes, signature_header: str) -> WebhookEvent:
        expected = hmac.new(self._webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            raise WebhookVerificationError("Razorpay webhook signature mismatch")

        event = json.loads(payload)
        event_type = event.get("event", "")
        normalized_type = _EVENT_TYPE_MAP.get(event_type, EVENT_UNKNOWN)
        payload_entity = (
            event.get("payload", {}).get("payment", {}).get("entity")
            or event.get("payload", {}).get("subscription", {}).get("entity")
            or event.get("payload", {}).get("refund", {}).get("entity")
            or {}
        )
        order_entity = event.get("payload", {}).get("order", {}).get("entity", {})
        notes = order_entity.get("notes") or payload_entity.get("notes") or {}
        return WebhookEvent(
            provider="razorpay", event_id=event.get("id", payload_entity.get("id", "")),
            event_type=normalized_type, raw_payload=event,
            provider_subscription_id=payload_entity.get("subscription_id") or (
                payload_entity.get("id") if "subscription" in event_type else None),
            provider_payment_id=payload_entity.get("id") if "payment" in event_type or "refund" in event_type else None,
            provider_customer_id=payload_entity.get("customer_id"),
            amount_cents=payload_entity.get("amount"),
            currency=payload_entity.get("currency"),
            status="succeeded" if "captured" in event_type or "charged" in event_type else
                   ("failed" if "failed" in event_type else None),
            tenant_id_hint=notes.get("tenant_id"),
            plan_code_hint=notes.get("plan_code"),
        )

    async def refund(self, provider_payment_id, amount_cents=None) -> RefundResult:
        client = self._client()
        try:
            kwargs = {} if amount_cents is None else {"amount": amount_cents}
            refund = await asyncio.to_thread(client.payment.refund, provider_payment_id, kwargs)
        except Exception as e:
            raise PaymentError(str(e)) from e
        return RefundResult(provider_refund_id=refund["id"], amount_cents=refund["amount"], status=refund["status"])

    async def get_payment_status(self, provider_payment_id: str) -> str:
        client = self._client()
        try:
            payment = await asyncio.to_thread(client.payment.fetch, provider_payment_id)
        except Exception as e:
            raise PaymentError(str(e)) from e
        return {"captured": "succeeded", "failed": "failed", "authorized": "pending",
                "created": "pending", "refunded": "refunded"}.get(payment["status"], "pending")

    async def cancel_subscription(self, provider_subscription_id, at_period_end=True) -> None:
        client = self._client()
        try:
            await asyncio.to_thread(
                client.subscription.cancel, provider_subscription_id,
                {"cancel_at_cycle_end": 1 if at_period_end else 0},
            )
        except Exception as e:
            raise PaymentError(str(e)) from e
