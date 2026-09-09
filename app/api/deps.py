from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.deps import get_db
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security.jwt import TokenError, decode_access_token
from app.core.security.permissions import get_effective_permissions
from app.core.tenancy.context import get_tenant, set_user
from app.models.user import User
from app.repositories.user_repo import UserRepository

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")

    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as e:
        raise UnauthorizedError(str(e)) from e

    import uuid

    user_id = uuid.UUID(payload["sub"])
    token_tenant_id = uuid.UUID(payload["tenant_id"])

    # The tenant resolved from the request (subdomain/header) must match
    # the tenant embedded in the token — prevents a token issued for
    # tenant A being replayed against tenant B's subdomain.
    if token_tenant_id != get_tenant():
        raise UnauthorizedError("Token does not match tenant context")

    user = await UserRepository(db).get_by_id(user_id, tenant_id=token_tenant_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive")

    set_user(user.id)
    request.state.current_user = user
    return user


def require_permission(code: str):
    async def checker(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if current_user.is_superadmin:
            return current_user
        perms = await get_effective_permissions(current_user.id, get_tenant(), db)
        if code not in perms:
            raise ForbiddenError(f"Missing permission: {code}")
        return current_user

    return checker
