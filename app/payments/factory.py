"""Provider selection. Supports per-region routing (Razorpay for
India/UPI-heavy tenants, Stripe for US/EU, PayPal as a checkout-page
alternative) rather than a single global provider — get_payment_provider
takes an optional region and falls back to PAYMENT_PROVIDER when no
region-specific mapping applies."""
from functools import lru_cache

from app.config import settings
from app.payments.base import PaymentProvider

_REGION_OVERRIDES = {
    # e.g. "IN": "razorpay", "US": "stripe", "EU": "stripe" — populate
    # from settings.PAYMENT_REGION_OVERRIDES (a JSON-configurable dict)
    # rather than hardcoding here, so ops can change routing without a
    # code deploy.
}


@lru_cache
def _build_provider(name: str) -> PaymentProvider:
    if name == "stripe":
        from app.payments.providers.stripe_provider import StripePaymentProvider
        return StripePaymentProvider(secret_key=settings.STRIPE_SECRET_KEY,
                                      webhook_secret=settings.STRIPE_WEBHOOK_SECRET)
    if name == "razorpay":
        from app.payments.providers.razorpay_provider import RazorpayPaymentProvider
        return RazorpayPaymentProvider(key_id=settings.RAZORPAY_KEY_ID, key_secret=settings.RAZORPAY_KEY_SECRET,
                                        webhook_secret=settings.RAZORPAY_WEBHOOK_SECRET)
    if name == "paypal":
        from app.payments.providers.paypal_provider import PayPalPaymentProvider
        return PayPalPaymentProvider(client_id=settings.PAYPAL_CLIENT_ID,
                                      client_secret=settings.PAYPAL_CLIENT_SECRET,
                                      webhook_id=settings.PAYPAL_WEBHOOK_ID,
                                      sandbox=settings.PAYPAL_SANDBOX)
    raise RuntimeError(f"Unknown PAYMENT_PROVIDER: {name!r}")


def get_payment_provider(region: str | None = None) -> PaymentProvider:
    provider_name = _REGION_OVERRIDES.get(region, settings.PAYMENT_PROVIDER) if region else settings.PAYMENT_PROVIDER
    return _build_provider(provider_name)


def get_provider_by_name(name: str) -> PaymentProvider:
    """Used by the webhook route, which must dispatch based on the
    provider named in the URL path — not the tenant's default — since a
    webhook could arrive for a payment made through any configured
    provider, not just whichever is currently the default."""
    return _build_provider(name)
