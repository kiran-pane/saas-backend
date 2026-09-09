"""PayPal provider (REST API v2 — Orders/Subscriptions). PayPal uses
OAuth2 client-credentials for API auth (token cached briefly, not
per-request) and webhook verification via its dedicated
verify-webhook-signature endpoint rather than a local HMAC check."""
import time

import httpx

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
    "PAYMENT.SALE.COMPLETED": EVENT_PAYMENT_SUCCEEDED,
    "PAYMENT.CAPTURE.COMPLETED": EVENT_PAYMENT_SUCCEEDED,
    "PAYMENT.SALE.DENIED": EVENT_PAYMENT_FAILED,
    "BILLING.SUBSCRIPTION.UPDATED": EVENT_SUBSCRIPTION_UPDATED,
    "BILLING.SUBSCRIPTION.CANCELLED": EVENT_SUBSCRIPTION_CANCELED,
    "PAYMENT.SALE.REFUNDED": EVENT_REFUND_ISSUED,
}


class PayPalPaymentProvider(PaymentProvider):
    def __init__(self, client_id: str, client_secret: str, webhook_id: str, sandbox: bool = False):
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_id = webhook_id
        self._base_url = "https://api-m.sandbox.paypal.com" if sandbox else "https://api-m.paypal.com"
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
            )
        if resp.status_code != 200:
            raise PaymentError(f"PayPal auth failed: {resp.status_code}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"]
        return self._token

    async def create_checkout_session(self, tenant_id, plan_code, amount_cents, currency,
                                       success_url, cancel_url, customer_email=None) -> CheckoutSession:
        token = await self._get_access_token()
        amount_str = f"{amount_cents / 100:.2f}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/v2/checkout/orders",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "intent": "CAPTURE",
                    "purchase_units": [{
                        "amount": {"currency_code": currency.upper(), "value": amount_str},
                        "custom_id": f"{tenant_id}:{plan_code}",
                    }],
                    "application_context": {"return_url": success_url, "cancel_url": cancel_url},
                },
            )
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal order creation failed: {resp.text}")
        order = resp.json()
        approve_url = next((link["href"] for link in order["links"] if link["rel"] == "approve"), "")
        return CheckoutSession(provider="paypal", checkout_url=approve_url, provider_session_id=order["id"])

    async def verify_webhook(self, payload: bytes, signature_header: str) -> WebhookEvent:
        # signature_header carries the full set of PayPal verification
        # headers, JSON-encoded by the caller (see webhook route) since
        # PayPal's verify-signature call needs several headers, not one.
        import json as _json

        headers = _json.loads(signature_header)
        token = await self._get_access_token()
        event = _json.loads(payload)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "transmission_id": headers.get("paypal-transmission-id"),
                    "transmission_time": headers.get("paypal-transmission-time"),
                    "cert_url": headers.get("paypal-cert-url"),
                    "auth_algo": headers.get("paypal-auth-algo"),
                    "transmission_sig": headers.get("paypal-transmission-sig"),
                    "webhook_id": self._webhook_id,
                    "webhook_event": event,
                },
            )
        if resp.status_code >= 400 or resp.json().get("verification_status") != "SUCCESS":
            raise WebhookVerificationError("PayPal webhook signature verification failed")

        resource = event.get("resource", {})
        normalized_type = _EVENT_TYPE_MAP.get(event.get("event_type", ""), EVENT_UNKNOWN)
        amount = resource.get("amount", {})
        custom_id = resource.get("custom_id") or ""
        tenant_id_hint, _, plan_code_hint = custom_id.partition(":")
        return WebhookEvent(
            provider="paypal", event_id=event["id"], event_type=normalized_type, raw_payload=event,
            provider_subscription_id=resource.get("id") if "SUBSCRIPTION" in event.get("event_type", "") else None,
            provider_payment_id=resource.get("id"),
            provider_customer_id=resource.get("payer", {}).get("payer_id"),
            amount_cents=int(float(amount.get("total") or amount.get("value") or 0) * 100) or None,
            currency=amount.get("currency"),
            status="succeeded" if resource.get("state") == "completed" or resource.get("status") == "COMPLETED" else None,
            tenant_id_hint=tenant_id_hint or None,
            plan_code_hint=plan_code_hint or None,
        )

    async def refund(self, provider_payment_id, amount_cents=None) -> RefundResult:
        token = await self._get_access_token()
        body = {}
        if amount_cents is not None:
            body = {"amount": {"value": f"{amount_cents / 100:.2f}", "currency_code": "USD"}}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/v2/payments/captures/{provider_payment_id}/refund",
                headers={"Authorization": f"Bearer {token}"}, json=body,
            )
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal refund failed: {resp.text}")
        data = resp.json()
        return RefundResult(
            provider_refund_id=data["id"],
            amount_cents=int(float(data.get("amount", {}).get("value", 0)) * 100),
            status=data.get("status", "").lower(),
        )

    async def get_payment_status(self, provider_payment_id: str) -> str:
        token = await self._get_access_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/v2/payments/captures/{provider_payment_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal status lookup failed: {resp.text}")
        status = resp.json().get("status", "")
        return {"COMPLETED": "succeeded", "DECLINED": "failed", "PENDING": "pending",
                "REFUNDED": "refunded"}.get(status, "pending")

    async def cancel_subscription(self, provider_subscription_id, at_period_end=True) -> None:
        # PayPal subscriptions don't support "cancel at period end" —
        # cancellation is immediate. at_period_end is accepted for
        # interface parity but has no effect on this provider; document
        # this clearly to callers/product rather than silently ignoring it.
        token = await self._get_access_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/v1/billing/subscriptions/{provider_subscription_id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
                json={"reason": "Canceled by tenant"},
            )
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal subscription cancel failed: {resp.text}")
