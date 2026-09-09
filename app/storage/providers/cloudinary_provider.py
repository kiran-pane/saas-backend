"""Cloudinary provider. Uses type="authenticated" (never the default
"upload" type, which serves assets on a PUBLIC CDN URL with no auth at
all — using the default would be a direct tenant-data leak) and signed
delivery URLs for every read."""
import asyncio
import time
from datetime import datetime, timedelta
from typing import AsyncIterator

from app.storage.base import (
    MAX_PRESIGNED_READ_TTL,
    MAX_PRESIGNED_WRITE_TTL,
    ObjectMetadata,
    ObjectNotFoundError,
    PresignedUrl,
    PresignedUrlPermission,
    StorageError,
    StorageKey,
    StorageProvider,
    UploadResult,
)


class CloudinaryStorageProvider(StorageProvider):
    def __init__(self, cloud_name: str, api_key: str, api_secret: str):
        self._cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._configured = False

    def _ensure_configured(self):
        if not self._configured:
            import cloudinary
            cloudinary.config(cloud_name=self._cloud_name, api_key=self._api_key,
                               api_secret=self._api_secret, secure=True)
            self._configured = True

    def _public_id(self, key: StorageKey) -> str:
        prefix = "quarantine/" if key.quarantine else ""
        return f"{key.tenant_id}/{key.document_id}/{prefix}{key._safe_filename()}"

    async def upload(self, key, data, content_type, size_bytes=None) -> UploadResult:
        import cloudinary.uploader

        self._ensure_configured()
        body = data if isinstance(data, (bytes, bytearray)) else \
            (data.read() if hasattr(data, "read") else b"".join([c async for c in data]))
        try:
            result = await asyncio.to_thread(
                cloudinary.uploader.upload, body, public_id=self._public_id(key),
                resource_type="auto", type="authenticated", overwrite=True, unique_filename=False,
                context={"tenant_id": key.tenant_id, "document_id": key.document_id},
            )
        except Exception as e:  # cloudinary.exceptions.Error subclasses Exception
            raise StorageError(str(e)) from e
        return UploadResult(key, result.get("bytes", len(body)), result.get("etag", ""), content_type, "cloudinary")

    async def download(self, key) -> AsyncIterator[bytes]:
        import httpx

        url = (await self.generate_presigned_url(key, PresignedUrlPermission.READ)).url
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    raise ObjectNotFoundError(key.path())
                if resp.status_code >= 400:
                    raise StorageError(f"Cloudinary fetch failed: {resp.status_code}")
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    yield chunk

    async def delete(self, key) -> None:
        import cloudinary.uploader

        self._ensure_configured()
        await asyncio.to_thread(cloudinary.uploader.destroy, self._public_id(key),
                                 resource_type="image", type="authenticated", invalidate=True)

    async def generate_presigned_url(self, key, permission, expires_in=timedelta(minutes=15),
                                      content_type=None) -> PresignedUrl:
        import cloudinary
        import cloudinary.utils

        self._ensure_configured()
        cap = MAX_PRESIGNED_WRITE_TTL if permission == PresignedUrlPermission.WRITE else MAX_PRESIGNED_READ_TTL
        if expires_in > cap:
            expires_in = cap

        if permission == PresignedUrlPermission.WRITE:
            timestamp = int(time.time())
            params = {"timestamp": timestamp, "public_id": self._public_id(key), "type": "authenticated"}
            signature = cloudinary.utils.api_sign_request(params, self._api_secret)
            url = f"https://api.cloudinary.com/v1_1/{self._cloud_name}/auto/upload"
            return PresignedUrl(
                url, datetime.utcfromtimestamp(timestamp) + expires_in, "POST",
                extra_fields={**params, "signature": signature, "api_key": self._api_key},
            )

        expires_at = datetime.utcnow() + expires_in
        url, _ = cloudinary.utils.cloudinary_url(
            self._public_id(key), type="authenticated", sign_url=True, resource_type="image",
            auth_token={"duration": int(expires_in.total_seconds())},
        )
        return PresignedUrl(url, expires_at, "GET")

    async def list_objects(self, tenant_id, prefix="") -> list[ObjectMetadata]:
        import cloudinary.api

        self._ensure_configured()
        result = await asyncio.to_thread(
            cloudinary.api.resources, type="authenticated", prefix=f"{tenant_id}/{prefix}", max_results=500,
        )
        return [_to_metadata(r, tenant_id) for r in result.get("resources", [])]

    async def get_metadata(self, key) -> ObjectMetadata:
        import cloudinary.api
        import cloudinary.exceptions

        self._ensure_configured()
        try:
            result = await asyncio.to_thread(
                cloudinary.api.resource, self._public_id(key), resource_type="image", type="authenticated",
            )
        except cloudinary.exceptions.NotFound as e:
            raise ObjectNotFoundError(key.path()) from e
        return _to_metadata(result, key.tenant_id)

    async def copy(self, source, dest) -> None:
        # Cloudinary has no server-side copy primitive across public_ids —
        # download + re-upload is the only path.
        chunks = [c async for c in self.download(source)]
        await self.upload(dest, b"".join(chunks), content_type="application/octet-stream")

    # Cloudinary has no native multipart upload primitive for this use
    # case — large files should route through a different provider, or a
    # future enhancement can buffer parts and upload once on complete
    # (same pattern as the local filesystem provider).


def _to_metadata(resource: dict, tenant_id: str) -> ObjectMetadata:
    public_id = resource["public_id"]
    parts = public_id[len(tenant_id) + 1:].split("/")
    document_id = parts[0] if parts else ""
    filename = parts[-1] if parts else public_id
    quarantine = len(parts) > 1 and parts[1] == "quarantine"
    return ObjectMetadata(
        key=StorageKey(tenant_id, document_id, filename, quarantine=quarantine),
        size_bytes=resource.get("bytes", 0), content_type=resource.get("format", ""),
        etag=resource.get("etag"), last_modified=datetime.fromisoformat(
            resource["created_at"].replace("Z", "+00:00")) if resource.get("created_at") else datetime.utcnow(),
    )
