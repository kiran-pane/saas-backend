import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=256)  # upper bound guards against bcrypt/argon2 DoS-by-length


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class GoogleAuthRequest(BaseModel):
    # ID token obtained by the frontend from Google's Identity Services
    # JS SDK (google.accounts.id). Verified server-side — see
    # app/core/security/google_oauth.py — never trusted as-is.
    id_token: str = Field(min_length=1, max_length=4096)


class GoogleAuthCodeRequest(BaseModel):
    # Only used for the server-driven authorization-code flow (backend
    # itself redirects to Google and receives this `code` on callback).
    code: str = Field(min_length=1, max_length=2048)


class CurrentUser(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    is_superadmin: bool = False
