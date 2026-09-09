"""Google Cloud Storage provider. The google-cloud-storage client is
sync, so every call runs in a thread (asyncio.to_thread) rather than
blocking the event loop — same pattern used for every sync cloud SDK
in this package."""
import asyncio
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


class GCSStorageProvider(StorageProvider):
    def __init__(self, bucket_name: str, credentials_path: str | None = None, kms_key_name: str | None = None):
        self._bucket_name = bucket_name
        self._credentials_path = credentials_path
        self._kms_key_name = kms_key_name
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import storage as gcs_storage
            self._client = gcs_storage.Client.from_service_account_json(self._credentials_path) \
                if self._credentials_path else gcs_storage.Client()
        return self._client

    def _bucket(self):
        return self._get_client().bucket(self._bucket_name)

    async def upload(self, key, data, content_type, size_bytes=None) -> UploadResult:
        from google.api_core.exceptions import GoogleAPICallError
        import io

        blob = self._bucket().blob(key.path())
        blob.metadata = {"tenant_id": key.tenant_id, "document_id": key.document_id}
        if self._kms_key_name:
            blob.kms_key_name = self._kms_key_name

        body = data if isinstance(data, (bytes, bytearray)) else \
            (data.read() if hasattr(data, "read") else b"".join([c async for c in data]))
        try:
            await asyncio.to_thread(blob.upload_from_file, io.BytesIO(body), content_type=content_type)
        except GoogleAPICallError as e:
            raise StorageError(str(e)) from e
        return UploadResult(key, len(body), blob.etag or "", content_type, "gcs")

    async def download(self, key) -> AsyncIterator[bytes]:
        from google.api_core.exceptions import NotFound

        blob = self._bucket().blob(key.path())
        try:
            body = await asyncio.to_thread(blob.download_as_bytes)
        except NotFound as e:
            raise ObjectNotFoundError(key.path()) from e
        chunk_size = 1024 * 1024
        for i in range(0, len(body), chunk_size):
            yield body[i:i + chunk_size]

    async def delete(self, key) -> None:
        from google.api_core.exceptions import NotFound

        blob = self._bucket().blob(key.path())
        try:
            await asyncio.to_thread(blob.delete)
        except NotFound:
            pass  # idempotent by design

    async def generate_presigned_url(self, key, permission, expires_in=timedelta(minutes=15),
                                      content_type=None) -> PresignedUrl:
        cap = MAX_PRESIGNED_WRITE_TTL if permission == PresignedUrlPermission.WRITE else MAX_PRESIGNED_READ_TTL
        if expires_in > cap:
            expires_in = cap

        blob = self._bucket().blob(key.path())
        method = "PUT" if permission == PresignedUrlPermission.WRITE else "GET"
        url = await asyncio.to_thread(
            blob.generate_signed_url, version="v4", expiration=expires_in, method=method,
            content_type=content_type if method == "PUT" else None,
        )
        return PresignedUrl(url, datetime.utcnow() + expires_in, method)

    async def list_objects(self, tenant_id, prefix="") -> list[ObjectMetadata]:
        full_prefix = f"{tenant_id}/{prefix}"
        blobs = await asyncio.to_thread(lambda: list(self._get_client().list_blobs(
            self._bucket_name, prefix=full_prefix)))
        return [_to_metadata(b, tenant_id) for b in blobs]

    async def get_metadata(self, key) -> ObjectMetadata:
        blob = self._bucket().blob(key.path())
        exists = await asyncio.to_thread(blob.exists)
        if not exists:
            raise ObjectNotFoundError(key.path())
        await asyncio.to_thread(blob.reload)
        return ObjectMetadata(key=key, size_bytes=blob.size or 0, content_type=blob.content_type or "",
                               etag=blob.etag, last_modified=blob.updated,
                               storage_class=(blob.storage_class or "standard").lower())

    async def copy(self, source, dest) -> None:
        src_blob = self._bucket().blob(source.path())
        await asyncio.to_thread(self._bucket().copy_blob, src_blob, self._bucket(), dest.path())

    # GCS multipart maps onto resumable-upload sessions rather than S3-style
    # part numbers; a full implementation is a larger lift (ranged PUTs
    # against a session URI) — flagged as not yet implemented so callers
    # get a clear error rather than silently-wrong behavior.


def _to_metadata(blob, tenant_id: str) -> ObjectMetadata:
    parts = blob.name[len(tenant_id) + 1:].split("/")
    document_id = parts[0] if parts else ""
    filename = parts[-1] if parts else blob.name
    quarantine = len(parts) > 1 and parts[1] == "quarantine"
    return ObjectMetadata(
        key=StorageKey(tenant_id, document_id, filename, quarantine=quarantine),
        size_bytes=blob.size or 0, content_type=blob.content_type or "",
        etag=blob.etag, last_modified=blob.updated, storage_class=(blob.storage_class or "standard").lower(),
    )
