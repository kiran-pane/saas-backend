import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission
from app.core.db.deps import get_db
from app.core.ratelimit.limiter import rate_limit
from app.core.tenancy.context import get_tenant
from app.models.billing import Plan, Subscription
from app.models.user import User
from app.schemas.payments import (
    CancelSubscriptionRequest,
    CheckoutRequest,
    CheckoutResponse,
    PlanOut,
    SubscriptionOut,
)
from app.services import payment_service

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Plan).where(Plan.is_active.is_(True)))
    return list(result.scalars().all())


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(
    user: User = Depends(require_permission("billing.subscription.read")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Subscription).where(Subscription.tenant_id == get_tenant()))
    return list(result.scalars().all())


@router.post("/checkout", response_model=CheckoutResponse,
             dependencies=[Depends(rate_limit("billing.checkout", 10, 60))])
async def create_checkout(
    payload: CheckoutRequest,
    user: User = Depends(require_permission("billing.subscription.manage")),
    db: AsyncSession = Depends(get_db),
):
    session = await payment_service.start_checkout(
        db, get_tenant(), payload.plan_code, payload.success_url, payload.cancel_url,
        customer_email=user.email, region=payload.region,
    )
    await db.commit()
    return CheckoutResponse(
        provider=session.provider, checkout_url=session.checkout_url,
        provider_session_id=session.provider_session_id, extra_fields=session.extra_fields,
    )


@router.post("/subscriptions/{subscription_id}/cancel", response_model=SubscriptionOut)
async def cancel_subscription(
    subscription_id: uuid.UUID,
    payload: CancelSubscriptionRequest,
    user: User = Depends(require_permission("billing.subscription.manage")),
    db: AsyncSession = Depends(get_db),
):
    sub = await payment_service.cancel_subscription(
        db, get_tenant(), subscription_id, payload.at_period_end, user.id,
    )
    await db.commit()
    return sub
