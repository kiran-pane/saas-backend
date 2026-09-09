"""Azure Blob Storage provider (async client)."""
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


class AzureBlobStorageProvider(StorageProvider):
    def __init__(self, account_url: str, container: str, account_key: str):
        self._account_url = account_url
        self._container = container
        self._account_key = account_key
        self._account_name = account_url.split("//")[-1].split(".")[0]

    def _client(self):
        from azure.storage.blob.aio import BlobServiceClient
        return BlobServiceClient(account_url=self._account_url, credential=self._account_key)

    async def upload(self, key, data, content_type, size_bytes=None) -> UploadResult:
        from azure.core.exceptions import AzureError
        from azure.storage.blob import ContentSettings

        body = data if isinstance(data, (bytes, bytearray)) else \
            (data.read() if hasattr(data, "read") else b"".join([c async for c in data]))
        async with self._client() as client:
            blob = client.get_blob_client(self._container, key.path())
            try:
                resp = await blob.upload_blob(
                    body, overwrite=True, content_settings=ContentSettings(content_type=content_type),
                    metadata={"tenant_id": key.tenant_id, "document_id": key.document_id},
                )
            except AzureError as e:
                raise StorageError(str(e)) from e
            return UploadResult(key, len(body), resp.get("etag", "").strip('"'), content_type, "azure")

    async def download(self, key) -> AsyncIterator[bytes]:
        from azure.core.exceptions import ResourceNotFoundError

        async with self._client() as client:
            blob = client.get_blob_client(self._container, key.path())
            try:
                stream = await blob.download_blob()
            except ResourceNotFoundError as e:
                raise ObjectNotFoundError(key.path()) from e
            async for chunk in stream.chunks():
                yield chunk

    async def delete(self, key) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        async with self._client() as client:
            blob = client.get_blob_client(self._container, key.path())
            try:
                await blob.delete_blob()
            except ResourceNotFoundError:
                pass  # idempotent by design

    async def generate_presigned_url(self, key, permission, expires_in=timedelta(minutes=15),
                                      content_type=None) -> PresignedUrl:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        cap = MAX_PRESIGNED_WRITE_TTL if permission == PresignedUrlPermission.WRITE else MAX_PRESIGNED_READ_TTL
        if expires_in > cap:
            expires_in = cap

        perms = BlobSasPermissions(write=True, create=True) if permission == PresignedUrlPermission.WRITE \
            else BlobSasPermissions(read=True)
        expiry = datetime.utcnow() + expires_in
        sas = generate_blob_sas(
            account_name=self._account_name, container_name=self._container, blob_name=key.path(),
            account_key=self._account_key, permission=perms, expiry=expiry,
        )
        url = f"{self._account_url}/{self._container}/{key.path()}?{sas}"
        return PresignedUrl(url, expiry, "PUT" if permission == PresignedUrlPermission.WRITE else "GET")

    async def list_objects(self, tenant_id, prefix="") -> list[ObjectMetadata]:
        full_prefix = f"{tenant_id}/{prefix}"
        results = []
        async with self._client() as client:
            container = client.get_container_client(self._container)
            async for blob in container.list_blobs(name_starts_with=full_prefix):
                results.append(_to_metadata(blob, tenant_id))
        return results

    async def get_metadata(self, key) -> ObjectMetadata:
        from azure.core.exceptions import ResourceNotFoundError

        async with self._client() as client:
            blob = client.get_blob_client(self._container, key.path())
            try:
                props = await blob.get_blob_properties()
            except ResourceNotFoundError as e:
                raise ObjectNotFoundError(key.path()) from e
            return ObjectMetadata(
                key=key, size_bytes=props.size, content_type=props.content_settings.content_type or "",
                etag=props.etag.strip('"') if props.etag else None, last_modified=props.last_modified,
                storage_class=(props.blob_tier or "standard").lower(),
            )

    async def copy(self, source, dest) -> None:
        async with self._client() as client:
            src_blob = client.get_blob_client(self._container, source.path())
            dst_blob = client.get_blob_client(self._container, dest.path())
            await dst_blob.start_copy_from_url(src_blob.url)

    # ---- multipart (Azure "staged blocks") ----
    async def initiate_multipart(self, key, content_type) -> str:
        import uuid
        return str(uuid.uuid4())  # Azure blocks are addressed by block_id, not a server-side upload session

    async def upload_part(self, key, upload_id, part_number, data: bytes) -> str:
        import base64
        block_id = base64.b64encode(f"{upload_id}-{part_number:05d}".encode()).decode()
        async with self._client() as client:
            blob = client.get_blob_client(self._container, key.path())
            await blob.stage_block(block_id, data)
        return block_id

    async def complete_multipart(self, key, upload_id, parts: dict[int, str]) -> UploadResult:
        async with self._client() as client:
            blob = client.get_blob_client(self._container, key.path())
            block_list = [parts[n] for n in sorted(parts.keys())]
            await blob.commit_block_list(block_list)
            props = await blob.get_blob_properties()
            return UploadResult(key, props.size, props.etag or "", props.content_settings.content_type or "", "azure")

    async def abort_multipart(self, key, upload_id) -> None:
        # Uncommitted staged blocks expire automatically after 7 days on
        # Azure's side; nothing to explicitly clean up beyond that.
        pass


def _to_metadata(blob, tenant_id: str) -> ObjectMetadata:
    parts = blob.name[len(tenant_id) + 1:].split("/")
    document_id = parts[0] if parts else ""
    filename = parts[-1] if parts else blob.name
    quarantine = len(parts) > 1 and parts[1] == "quarantine"
    return ObjectMetadata(
        key=StorageKey(tenant_id, document_id, filename, quarantine=quarantine),
        size_bytes=blob.size, content_type="application/octet-stream",
        etag=blob.etag.strip('"') if blob.etag else None, last_modified=blob.last_modified,
        storage_class=(blob.blob_tier or "standard").lower(),
    )
