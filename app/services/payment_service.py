"""Payment/subscription business logic. Webhook events are the SOURCE OF
TRUTH for subscription/payment state — never the client-side
success_url redirect, which only improves UX. Processing is idempotent
(deduped on provider_event_id) since every provider retries webhook
delivery.

RLS note: subscriptions/payments are RLS-protected like every other
tenant table, but webhook processing has no request-scoped tenant
context (a webhook carries no X-Tenant-Slug header). Tenant resolution
therefore goes through two paths, in order:
  1. event.tenant_id_hint — present on checkout-completion-type events,
     echoed back from the metadata/notes/custom_id embedded when the
     CheckoutSession was created (see app/payments/providers/*).
  2. subscription_provider_index — a deliberately NON-RLS lookup table
     (see migration 0006), used for renewal/update events that don't
     carry the original hint, keyed on provider_subscription_id.
Once resolved, `SET LOCAL app.current_tenant` is issued before any
RLS-protected read/write — never attempt one before resolution, since an
RLS-protected SELECT with no session variable set simply returns zero
rows (fails closed, but silently), not an error.
"""
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.exceptions import PaymentError as AppPaymentError
from app.models.billing import Payment, PaymentWebhookEvent, Plan, Subscription, SubscriptionProviderIndex
from app.payments.base import (
    EVENT_PAYMENT_FAILED,
    EVENT_PAYMENT_SUCCEEDED,
    EVENT_REFUND_ISSUED,
    EVENT_SUBSCRIPTION_CANCELED,
    EVENT_SUBSCRIPTION_UPDATED,
    PaymentError,
    WebhookEvent,
    WebhookVerificationError,
)
from app.payments.factory import get_payment_provider, get_provider_by_name
from app.services.audit_service import record as audit_record


async def get_plan_by_code(db: AsyncSession, plan_code: str) -> Plan:
    result = await db.execute(select(Plan).where(Plan.code == plan_code, Plan.is_active.is_(True)))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Unknown or inactive plan code: {plan_code}")
    return plan


async def start_checkout(db: AsyncSession, tenant_id: uuid.UUID, plan_code: str, success_url: str,
                          cancel_url: str, customer_email: str | None, region: str | None = None):
    plan = await get_plan_by_code(db, plan_code)
    provider = get_payment_provider(region)
    try:
        session = await provider.create_checkout_session(
            tenant_id=str(tenant_id), plan_code=plan.code, amount_cents=plan.amount_cents,
            currency=plan.currency, success_url=success_url, cancel_url=cancel_url,
            customer_email=customer_email,
        )
    except PaymentError as e:
        raise AppPaymentError(str(e)) from e

    audit_record("billing.checkout.started", resource_type="plan", resource_id=str(plan.id),
                 metadata={"provider": session.provider, "provider_session_id": session.provider_session_id})
    return session


async def _set_session_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    await db.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)})


async def _resolve_tenant_id(db: AsyncSession, event: WebhookEvent) -> uuid.UUID | None:
    if event.tenant_id_hint:
        try:
            return uuid.UUID(event.tenant_id_hint)
        except ValueError:
            return None

    if event.provider_subscription_id:
        # Non-RLS lookup — safe to query with no session tenant variable set.
        result = await db.execute(
            select(SubscriptionProviderIndex.tenant_id).where(
                SubscriptionProviderIndex.provider == event.provider,
                SubscriptionProviderIndex.provider_subscription_id == event.provider_subscription_id,
            )
        )
        return result.scalar_one_or_none()

    return None


async def _ensure_subscription_link(db: AsyncSession, tenant_id: uuid.UUID, event: WebhookEvent) -> None:
    """Called once per (provider, provider_subscription_id) the first
    time we observe it — populates the non-RLS index table and
    creates/links the RLS-protected Subscription row. Safe to call
    repeatedly (upsert semantics)."""
    if not event.provider_subscription_id:
        return

    existing_index = await db.execute(
        select(SubscriptionProviderIndex).where(
            SubscriptionProviderIndex.provider == event.provider,
            SubscriptionProviderIndex.provider_subscription_id == event.provider_subscription_id,
        )
    )
    if existing_index.scalar_one_or_none() is None:
        db.add(SubscriptionProviderIndex(
            provider=event.provider, provider_subscription_id=event.provider_subscription_id,
            tenant_id=tenant_id,
        ))
        await db.flush()

    # Session tenant must already be set by the caller before this
    # RLS-protected SELECT/INSERT.
    existing_sub = await db.execute(
        select(Subscription).where(Subscription.provider == event.provider,
                                    Subscription.provider_subscription_id == event.provider_subscription_id)
    )
    if existing_sub.scalar_one_or_none() is not None:
        return

    plan = None
    if event.plan_code_hint:
        result = await db.execute(select(Plan).where(Plan.code == event.plan_code_hint))
        plan = result.scalar_one_or_none()
    if plan is None:
        # Fall back to the free plan rather than leaving plan_id NULL —
        # a webhook arriving for an unrecognized plan code shouldn't
        # crash processing; flag it via audit metadata for investigation.
        result = await db.execute(select(Plan).where(Plan.code == "free"))
        plan = result.scalar_one_or_none()

    sub = Subscription(
        tenant_id=tenant_id, plan_id=plan.id if plan else None, provider=event.provider,
        provider_subscription_id=event.provider_subscription_id,
        provider_customer_id=event.provider_customer_id, status="active",
    )
    db.add(sub)
    await db.flush()
    audit_record("billing.subscription.created", resource_type="subscription", resource_id=str(sub.id),
                 metadata={"provider": event.provider, "plan_code_hint": event.plan_code_hint,
                           "plan_resolved": plan.code if plan else None})


async def process_webhook(db: AsyncSession, provider_name: str, raw_payload: bytes,
                           signature_header: str) -> None:
    provider = get_provider_by_name(provider_name)

    # Never process an unverified payload — verification happens before
    # anything else touches the payload's contents.
    event = await provider.verify_webhook(raw_payload, signature_header)

    # Idempotency dedup — PaymentWebhookEvent has no RLS (global table),
    # safe to check/insert before tenant resolution.
    existing = await db.execute(
        select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider == provider_name,
            PaymentWebhookEvent.provider_event_id == event.event_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return  # already processed — return success without reprocessing

    webhook_row = PaymentWebhookEvent(
        provider=provider_name, provider_event_id=event.event_id, event_type=event.event_type,
        payload=event.raw_payload,
    )
    db.add(webhook_row)
    await db.flush()

    tenant_id = await _resolve_tenant_id(db, event)
    if tenant_id is None:
        # Can't resolve which tenant this belongs to (e.g. a renewal
        # event for a subscription we never saw the initial checkout
        # for) — store the raw event for ops review, but don't guess.
        webhook_row.processing_error = "Could not resolve tenant_id for this event"
        await db.flush()
        return

    try:
        await _set_session_tenant(db, tenant_id)
        if event.tenant_id_hint and event.provider_subscription_id:
            await _ensure_subscription_link(db, tenant_id, event)
        await _apply_webhook_event(db, tenant_id, event)
        webhook_row.processed = True
    except Exception as e:
        webhook_row.processing_error = str(e)[:1000]
        raise
    finally:
        await db.flush()


async def _apply_webhook_event(db: AsyncSession, tenant_id: uuid.UUID, event: WebhookEvent) -> None:
    if event.event_type == EVENT_PAYMENT_SUCCEEDED:
        await _record_payment(db, tenant_id, event, status="succeeded")
    elif event.event_type == EVENT_PAYMENT_FAILED:
        await _record_payment(db, tenant_id, event, status="failed")
    elif event.event_type == EVENT_SUBSCRIPTION_UPDATED:
        await _update_subscription(db, event)
    elif event.event_type == EVENT_SUBSCRIPTION_CANCELED:
        await _update_subscription(db, event, force_status="canceled")
    elif event.event_type == EVENT_REFUND_ISSUED:
        await _record_refund(db, event)
    # Unknown event types are stored (for audit/debugging) but produce no
    # state change — safer than guessing at an unrecognized event's intent.


async def _record_payment(db: AsyncSession, tenant_id: uuid.UUID, event: WebhookEvent, status: str) -> None:
    if not event.provider_payment_id:
        return

    existing = await db.execute(
        select(Payment).where(Payment.provider == event.provider,
                               Payment.provider_payment_id == event.provider_payment_id)
    )
    payment = existing.scalar_one_or_none()
    if payment is None:
        sub_result = await db.execute(
            select(Subscription.id).where(Subscription.provider == event.provider,
                                           Subscription.provider_subscription_id == event.provider_subscription_id)
        ) if event.provider_subscription_id else None
        subscription_id = sub_result.scalar_one_or_none() if sub_result else None

        payment = Payment(
            tenant_id=tenant_id, subscription_id=subscription_id, provider=event.provider,
            provider_payment_id=event.provider_payment_id, amount_cents=event.amount_cents or 0,
            currency=event.currency or "usd", status=status,
        )
        db.add(payment)
    else:
        payment.status = status
    await db.flush()

    audit_record("billing.payment." + status, resource_type="payment",
                 resource_id=event.provider_payment_id, metadata={"provider": event.provider})


async def _update_subscription(db: AsyncSession, event: WebhookEvent, force_status: str | None = None) -> None:
    if not event.provider_subscription_id:
        return
    result = await db.execute(
        select(Subscription).where(Subscription.provider == event.provider,
                                    Subscription.provider_subscription_id == event.provider_subscription_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        return  # subscription not yet linked locally — nothing to update
    sub.status = force_status or (event.status or sub.status)
    await db.flush()
    audit_record("billing.subscription.updated", resource_type="subscription", resource_id=str(sub.id),
                 metadata={"status": sub.status})


async def _record_refund(db: AsyncSession, event: WebhookEvent) -> None:
    if not event.provider_payment_id:
        return
    result = await db.execute(
        select(Payment).where(Payment.provider == event.provider,
                               Payment.provider_payment_id == event.provider_payment_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        return
    payment.status = "refunded"
    await db.flush()
    audit_record("billing.payment.refunded", resource_type="payment", resource_id=event.provider_payment_id)


async def cancel_subscription(db: AsyncSession, tenant_id: uuid.UUID, subscription_id: uuid.UUID,
                               at_period_end: bool, actor_user_id: uuid.UUID) -> Subscription:
    sub = await db.get(Subscription, subscription_id)
    if sub is None or sub.tenant_id != tenant_id:
        raise NotFoundError("Subscription not found")

    provider = get_provider_by_name(sub.provider)
    try:
        await provider.cancel_subscription(sub.provider_subscription_id, at_period_end)
    except PaymentError as e:
        raise AppPaymentError(str(e)) from e

    sub.cancel_at_period_end = at_period_end
    if not at_period_end:
        sub.status = "canceled"
    await db.flush()
    audit_record("billing.subscription.cancel_requested", resource_type="subscription",
                 resource_id=str(sub.id), actor_user_id=actor_user_id,
                 metadata={"at_period_end": at_period_end})
    return sub
