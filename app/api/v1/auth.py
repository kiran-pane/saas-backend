from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.deps import get_db
from app.core.ratelimit.limiter import check_custom_rate_limit, rate_limit
from app.core.security.google_oauth import exchange_code_for_tokens, verify_google_id_token
from app.core.tenancy.context import get_tenant
from app.schemas.auth import (
    GoogleAuthCodeRequest,
    GoogleAuthRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from app.services import auth_service
from app.services.audit_service import record as audit_record

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse,
             dependencies=[Depends(rate_limit("auth.login", 10, 60, by="ip"))])
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    # Per-email limiting IN ADDITION to the per-IP limit above — IP-based
    # limiting alone is trivially bypassed with a botnet/proxy pool
    # against a single known-valid email (credential stuffing). Evaluated
    # before the DB lookup so a locked-out email is rejected cheaply.
    await check_custom_rate_limit("auth.login.email", limit=5, window_seconds=900,
                                   identity=payload.email.lower())
    # Note: audit_service.record() enqueues to Celery/Redis immediately —
    # it does not depend on this request's SQLAlchemy transaction
    # committing, so a failed-login audit row is recorded even though
    # authenticate() raises (and get_db()'s dependency then rolls back
    # the DB session, which never touched anything besides the SELECT).
    user = await auth_service.authenticate(db, get_tenant(), payload.email, payload.password)
    access_token, refresh_token = await auth_service.issue_tokens(db, user, role_ids=[])
    audit_record("auth.login", resource_type="user", resource_id=str(user.id), actor_user_id=user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse,
             dependencies=[Depends(rate_limit("auth.refresh", 30, 60, by="ip"))])
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    access_token, refresh_token, user = await auth_service.rotate_refresh_token(db, payload.refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=204)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    await auth_service.revoke_refresh_token(db, payload.refresh_token)
    audit_record("auth.logout")


@router.post("/google", response_model=TokenResponse,
             dependencies=[Depends(rate_limit("auth.google", 20, 60, by="ip"))])
async def google_login(payload: GoogleAuthRequest, db: AsyncSession = Depends(get_db)):
    """Primary Google sign-in path: frontend sends the ID token it got
    from Google's JS SDK; we verify it server-side and issue our own
    access/refresh token pair, same as password login. Frontend never
    needs to know about GOOGLE_CLIENT_SECRET at all."""
    profile = verify_google_id_token(payload.id_token)
    user = await auth_service.login_or_register_with_google(db, get_tenant(), profile)
    access_token, refresh_token = await auth_service.issue_tokens(db, user, role_ids=[])
    audit_record("auth.login.google", resource_type="user", resource_id=str(user.id),
                 actor_user_id=user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/google/code", response_model=TokenResponse,
             dependencies=[Depends(rate_limit("auth.google", 20, 60, by="ip"))])
async def google_login_via_code(payload: GoogleAuthCodeRequest, db: AsyncSession = Depends(get_db)):
    """Alternate path for setups where the backend itself drives the
    OAuth redirect (no frontend JS SDK) — exchanges the authorization
    code for an ID token, then follows the same verify + login flow."""
    id_token_str = await exchange_code_for_tokens(payload.code)
    profile = verify_google_id_token(id_token_str)
    user = await auth_service.login_or_register_with_google(db, get_tenant(), profile)
    access_token, refresh_token = await auth_service.issue_tokens(db, user, role_ids=[])
    audit_record("auth.login.google", resource_type="user", resource_id=str(user.id),
                 actor_user_id=user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)
