"""Google Sign-In support.

Primary flow (recommended, simplest, what most Next.js frontends will
use): the frontend uses Google's Identity Services JS SDK, obtains a
signed Google ID token, and POSTs it to our backend at
`/api/v1/auth/google`. We verify the token's signature + audience +
issuer server-side via `verify_google_id_token` — the frontend is never
trusted to assert who the user is.

Secondary flow (only if you want the *backend* to drive the redirect,
e.g. no frontend JS SDK): `exchange_code_for_tokens` performs the
standard OAuth2 authorization-code exchange against Google's token
endpoint. Requires GOOGLE_CLIENT_SECRET + GOOGLE_REDIRECT_URI.
"""
from dataclasses import dataclass

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.config import settings
from app.core.exceptions import UnauthorizedError

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


@dataclass
class GoogleProfile:
    sub: str            # stable Google user id — use this as the identity key, never email
    email: str
    email_verified: bool
    name: str | None
    picture: str | None


def verify_google_id_token(id_token_str: str) -> GoogleProfile:
    """Verifies signature, expiry, issuer, and audience (must match our
    configured GOOGLE_CLIENT_ID) against Google's public keys. Raises
    UnauthorizedError on any failure — never partially trust a token
    that fails verification."""
    if not settings.GOOGLE_CLIENT_ID:
        raise UnauthorizedError("Google sign-in is not configured on this server")

    try:
        payload = google_id_token.verify_oauth2_token(
            id_token_str, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID
        )
    except ValueError as e:
        raise UnauthorizedError(f"Invalid Google ID token: {e}") from e

    if payload.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise UnauthorizedError("Invalid token issuer")

    if not payload.get("email_verified", False):
        raise UnauthorizedError("Google account email is not verified")

    return GoogleProfile(
        sub=payload["sub"],
        email=payload["email"],
        email_verified=True,
        name=payload.get("name"),
        picture=payload.get("picture"),
    )


async def exchange_code_for_tokens(code: str) -> str:
    """Authorization-code flow: exchanges a one-time `code` (from Google's
    redirect to GOOGLE_REDIRECT_URI) for an id_token. Only needed if the
    backend itself initiates the OAuth redirect rather than a frontend SDK."""
    if not (settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI):
        raise UnauthorizedError("Server-driven Google OAuth is not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        raise UnauthorizedError("Failed to exchange Google authorization code")

    return response.json()["id_token"]
