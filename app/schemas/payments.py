import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class CheckoutRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=50)
    success_url: str = Field(max_length=2048)
    cancel_url: str = Field(max_length=2048)
    region: str | None = Field(default=None, max_length=2)  # ISO country code, drives provider routing


class CheckoutResponse(BaseModel):
    provider: str
    checkout_url: str
    provider_session_id: str
    extra_fields: dict | None = None


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    provider: str
    status: str
    current_period_end: datetime | None
    cancel_at_period_end: bool

    class Config:
        from_attributes = True


class PlanOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    amount_cents: int
    currency: str
    interval: str

    class Config:
        from_attributes = True


class CancelSubscriptionRequest(BaseModel):
    at_period_end: bool = True
