"""Payment webhook receiver. Deliberately tenant-context-exempt (see
TenantMiddleware.EXEMPT_PATH_PREFIXES) — a webhook from Stripe/Razorpay/
PayPal carries no X-Tenant-Slug header, and tenant scoping is resolved
inside payment_service from provider-side metadata instead."""
import json

from fastapi import APIRouter, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.db.deps import get_db_no_tenant
from app.core.exceptions import UnauthorizedError
from app.payments.base import WebhookVerificationError
from app.services import payment_service

router = APIRouter(prefix="/webhooks/payments", tags=["webhooks"])


@router.post("/{provider}")
async def receive_payment_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db_no_tenant),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    paypal_transmission_id: str | None = Header(default=None, alias="Paypal-Transmission-Id"),
    paypal_transmission_time: str | None = Header(default=None, alias="Paypal-Transmission-Time"),
    paypal_cert_url: str | None = Header(default=None, alias="Paypal-Cert-Url"),
    paypal_auth_algo: str | None = Header(default=None, alias="Paypal-Auth-Algo"),
    paypal_transmission_sig: str | None = Header(default=None, alias="Paypal-Transmission-Sig"),
):
    raw_body = await request.body()

    if provider == "stripe":
        signature_header = stripe_signature or ""
    elif provider == "razorpay":
        signature_header = razorpay_signature or ""
    elif provider == "paypal":
        # PayPal's verification call needs several headers at once —
        # bundle them as JSON so PayPalPaymentProvider.verify_webhook can
        # unpack them without changing the shared interface's signature.
        signature_header = json.dumps({
            "paypal-transmission-id": paypal_transmission_id,
            "paypal-transmission-time": paypal_transmission_time,
            "paypal-cert-url": paypal_cert_url,
            "paypal-auth-algo": paypal_auth_algo,
            "paypal-transmission-sig": paypal_transmission_sig,
        })
    else:
        raise UnauthorizedError(f"Unknown payment provider: {provider}")

    try:
        await payment_service.process_webhook(db, provider, raw_body, signature_header)
        await db.commit()
    except WebhookVerificationError:
        await db.rollback()
        # 401, not 400 — signals "don't retry this exact payload," which
        # is correct for a verification failure (retrying an unsigned/
        # forged payload should never succeed).
        raise UnauthorizedError("Webhook signature verification failed")

    return {"status": "processed"}
