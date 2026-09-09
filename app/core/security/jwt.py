import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings


class TokenError(Exception):
    pass


def create_access_token(user_id: uuid.UUID, tenant_id: uuid.UUID, role_ids: list[str]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "roles": role_ids,  # kept small; effective *permissions* are resolved
                            # server-side per request (see permissions.py) so
                            # revocation is immediate, not "wait for token expiry"
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.APP_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.APP_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("Access token expired") from e
    except jwt.InvalidTokenError as e:
        raise TokenError("Invalid access token") from e

    if payload.get("type") != "access":
        raise TokenError("Wrong token type")
    return payload
