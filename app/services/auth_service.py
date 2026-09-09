import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security.google_oauth import GoogleProfile
from app.core.security.jwt import create_access_token
from app.core.security.password import hash_password, verify_password
from app.models.user import RefreshToken, User
from app.repositories.user_repo import RefreshTokenRepository, UserRepository


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def register_user(db: AsyncSession, tenant_id: uuid.UUID, email: str, password: str,
                         full_name: str | None = None) -> User:
    from app.services.audit_service import record as audit_record

    repo = UserRepository(db)
    if await repo.get_by_email(email, tenant_id):
        from app.core.exceptions import ConflictError
        raise ConflictError("A user with this email already exists")

    user = User(
        tenant_id=tenant_id, email=email, hashed_password=hash_password(password),
        full_name=full_name,
    )
    user = await repo.add(user)
    audit_record("user.created", resource_type="user", resource_id=str(user.id))
    return user


async def authenticate(db: AsyncSession, tenant_id: uuid.UUID, email: str, password: str) -> User:
    from app.services.audit_service import record as audit_record

    repo = UserRepository(db)
    user = await repo.get_by_email(email, tenant_id)
    if user is None or not user.is_active or user.hashed_password is None:
        # Same generic error whether the user doesn't exist, is inactive,
        # or is an OAuth-only account — never leak which case it was.
        # Audit metadata deliberately omits the attempted password and
        # only truncates/hashes the email rather than storing it raw, to
        # avoid writing PII into a long-retention audit table for an
        # attempt that may not even correspond to a real account.
        audit_record("auth.login.failed", resource_type="user", metadata={"email_domain": email.split("@")[-1] if "@" in email else None})
        raise UnauthorizedError("Invalid email or password")
    if not verify_password(password, user.hashed_password):
        audit_record("auth.login.failed", resource_type="user", resource_id=str(user.id))
        raise UnauthorizedError("Invalid email or password")
    return user


async def login_or_register_with_google(
    db: AsyncSession, tenant_id: uuid.UUID, profile: GoogleProfile
) -> User:
    """Find-or-create by (tenant_id, oauth_provider, oauth_sub) first —
    Google's `sub` is the stable identity key, never the email (emails
    can be reassigned by the IdP). Falls back to matching an existing
    password-based account by email and linking it, so a user who
    originally signed up with a password can also sign in with Google
    afterwards without ending up with two separate accounts."""
    from sqlalchemy import select

    existing_by_sub = await db.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.oauth_provider == "google",
            User.oauth_sub == profile.sub,
        )
    )
    user = existing_by_sub.scalar_one_or_none()
    if user is not None:
        if not user.is_active:
            raise ForbiddenError("Account is inactive")
        return user

    repo = UserRepository(db)
    user = await repo.get_by_email(profile.email, tenant_id)
    if user is not None:
        if not user.is_active:
            raise ForbiddenError("Account is inactive")
        # Link the Google identity to the existing account.
        user.oauth_provider = "google"
        user.oauth_sub = profile.sub
        await db.flush()
        return user

    user = User(
        tenant_id=tenant_id,
        email=profile.email,
        hashed_password=None,
        full_name=profile.name,
        oauth_provider="google",
        oauth_sub=profile.sub,
    )
    user = await repo.add(user)
    from app.services.audit_service import record as audit_record
    audit_record("user.created", resource_type="user", resource_id=str(user.id),
                 metadata={"via": "google_oauth"})
    return user


async def issue_tokens(db: AsyncSession, user: User, role_ids: list[str]) -> tuple[str, str]:
    access_token = create_access_token(user.id, user.tenant_id, role_ids)

    raw_refresh = str(uuid.uuid4()) + str(uuid.uuid4())
    family_id = uuid.uuid4()
    token_repo = RefreshTokenRepository(db)
    await token_repo.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(raw_refresh),
            family_id=family_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    return access_token, raw_refresh


async def rotate_refresh_token(db: AsyncSession, raw_refresh: str) -> tuple[str, str, User]:
    """Validates + rotates a refresh token. If the presented token was
    already revoked (i.e. it's being replayed after a legitimate rotation
    already happened), the entire token family is revoked — this is the
    signal that a refresh token was stolen, and the fix is to force
    re-authentication rather than let the thief keep refreshing forever."""
    token_repo = RefreshTokenRepository(db)
    token_hash = _hash_token(raw_refresh)
    token = await token_repo.get_by_hash(token_hash)

    if token is None:
        raise UnauthorizedError("Invalid refresh token")

    if token.revoked_at is not None:
        await token_repo.revoke_family(token.family_id)
        from app.services.audit_service import record as audit_record
        audit_record(
            "auth.refresh_token.reuse_detected", resource_type="user",
            resource_id=str(token.user_id), actor_user_id=token.user_id,
            metadata={"family_id": str(token.family_id)},
        )
        raise UnauthorizedError("Refresh token reuse detected — all sessions revoked")

    if token.expires_at < datetime.now(timezone.utc):
        raise UnauthorizedError("Refresh token expired")

    from sqlalchemy import func

    token.revoked_at = func.now()
    await db.flush()

    from app.repositories.user_repo import UserRepository as _UserRepo

    user = await _UserRepo(db).get_by_id(token.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User no longer active")

    new_raw_refresh = str(uuid.uuid4()) + str(uuid.uuid4())
    await token_repo.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(new_raw_refresh),
            family_id=token.family_id,  # same family — rotation, not a new session
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    access_token = create_access_token(user.id, user.tenant_id, [])
    return access_token, new_raw_refresh, user


async def revoke_refresh_token(db: AsyncSession, raw_refresh: str) -> None:
    token_repo = RefreshTokenRepository(db)
    token = await token_repo.get_by_hash(_hash_token(raw_refresh))
    if token:
        await token_repo.revoke_family(token.family_id)
