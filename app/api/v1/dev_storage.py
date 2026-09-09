"""Local-filesystem storage provider's presigned-URL target. Only
meaningful when STORAGE_PROVIDER=local (dev/test) — the factory already
refuses to select the local provider when ENV=production, so this route
is unreachable via a real presigned URL in production regardless."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.storage.base import ObjectNotFoundError, PresignedUrlPermission, StorageKey
from app.storage.providers.local import LocalFilesystemStorageProvider

router = APIRouter(prefix="/dev-storage", tags=["dev-storage"])


def _decode_token(token: str) -> dict:
    import jwt

    try:
        return jwt.decode(token, settings.APP_SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        raise ForbiddenError(f"Invalid or expired dev-storage token: {e}")


@router.get("")
async def dev_storage_get(token: str = Query(...)):
    if settings.STORAGE_PROVIDER != "local":
        raise ValidationAppError("dev-storage is only active when STORAGE_PROVIDER=local")
    payload = _decode_token(token)
    if payload.get("perm") != PresignedUrlPermission.READ.value:
        raise ForbiddenError("Token is not valid for read access")

    provider = LocalFilesystemStorageProvider(root_dir=settings.LOCAL_STORAGE_ROOT)
    parts = payload["path"].split("/", 2)
    key = StorageKey(parts[0], parts[1], parts[2])
    try:
        async def stream():
            async for chunk in provider.download(key):
                yield chunk
        return StreamingResponse(stream(), media_type="application/octet-stream")
    except ObjectNotFoundError:
        raise NotFoundError("Object not found")


@router.put("")
async def dev_storage_put(request: Request, token: str = Query(...)):
    if settings.STORAGE_PROVIDER != "local":
        raise ValidationAppError("dev-storage is only active when STORAGE_PROVIDER=local")
    payload = _decode_token(token)
    if payload.get("perm") != PresignedUrlPermission.WRITE.value:
        raise ForbiddenError("Token is not valid for write access")

    provider = LocalFilesystemStorageProvider(root_dir=settings.LOCAL_STORAGE_ROOT)
    parts = payload["path"].split("/", 2)
    key = StorageKey(parts[0], parts[1], parts[2])
    body = await request.body()
    await provider.upload(key, body, request.headers.get("content-type", "application/octet-stream"))
    return Response(status_code=204)
