"""AWS S3 provider. Also works against any S3-compatible endpoint
(MinIO, Cloudflare R2) by setting S3_ENDPOINT_URL, which is how local
integration testing against a real S3 API (rather than the local
filesystem stub) is done without touching real AWS."""
from datetime import datetime, timedelta
from typing import AsyncIterator, BinaryIO

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


class S3StorageProvider(StorageProvider):
    def __init__(self, bucket: str, region: str, endpoint_url: str | None = None,
                 kms_key_id: str | None = None):
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url or None
        self._kms_key_id = kms_key_id

    def _session(self):
        import aioboto3
        return aioboto3.Session()

    async def upload(self, key, data, content_type, size_bytes=None) -> UploadResult:
        from botocore.exceptions import ClientError

        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            try:
                body = data if isinstance(data, (bytes, bytearray)) else \
                    (data.read() if hasattr(data, "read") else b"".join([c async for c in data]))
                kwargs = dict(
                    Bucket=self._bucket, Key=key.path(), Body=body, ContentType=content_type,
                    Metadata={"tenant_id": key.tenant_id, "document_id": key.document_id},
                )
                if self._kms_key_id:
                    kwargs["ServerSideEncryption"] = "aws:kms"
                    kwargs["SSEKMSKeyId"] = self._kms_key_id
                else:
                    kwargs["ServerSideEncryption"] = "AES256"
                resp = await s3.put_object(**kwargs)
                return UploadResult(key, len(body), resp["ETag"].strip('"'), content_type, "s3")
            except ClientError as e:
                raise StorageError(str(e)) from e

    async def download(self, key) -> AsyncIterator[bytes]:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            try:
                obj = await s3.get_object(Bucket=self._bucket, Key=key.path())
            except s3.exceptions.NoSuchKey as e:
                raise ObjectNotFoundError(key.path()) from e
            except s3.exceptions.ClientError as e:
                if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                    raise ObjectNotFoundError(key.path()) from e
                raise StorageError(str(e)) from e
            async for chunk in obj["Body"].iter_chunks(chunk_size=1024 * 1024):
                yield chunk

    async def delete(self, key) -> None:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key.path())  # idempotent by S3 API design

    async def generate_presigned_url(self, key, permission, expires_in=timedelta(minutes=15),
                                      content_type=None) -> PresignedUrl:
        cap = MAX_PRESIGNED_WRITE_TTL if permission == PresignedUrlPermission.WRITE else MAX_PRESIGNED_READ_TTL
        if expires_in > cap:
            expires_in = cap

        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            client_method = "put_object" if permission == PresignedUrlPermission.WRITE else "get_object"
            params = {"Bucket": self._bucket, "Key": key.path()}
            if content_type and permission == PresignedUrlPermission.WRITE:
                params["ContentType"] = content_type
            url = await s3.generate_presigned_url(
                ClientMethod=client_method, Params=params, ExpiresIn=int(expires_in.total_seconds()),
            )
            return PresignedUrl(url, datetime.utcnow() + expires_in,
                                 "PUT" if permission == PresignedUrlPermission.WRITE else "GET")

    async def list_objects(self, tenant_id, prefix="") -> list[ObjectMetadata]:
        full_prefix = f"{tenant_id}/{prefix}"
        session = self._session()
        results = []
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
                for obj in page.get("Contents", []):
                    results.append(_to_metadata(obj, tenant_id))
        return results

    async def get_metadata(self, key) -> ObjectMetadata:
        from botocore.exceptions import ClientError

        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            try:
                resp = await s3.head_object(Bucket=self._bucket, Key=key.path())
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                    raise ObjectNotFoundError(key.path()) from e
                raise StorageError(str(e)) from e
            return ObjectMetadata(
                key=key, size_bytes=resp["ContentLength"], content_type=resp.get("ContentType", ""),
                etag=resp.get("ETag", "").strip('"'), last_modified=resp["LastModified"],
                storage_class=resp.get("StorageClass", "STANDARD").lower(),
            )

    async def copy(self, source, dest) -> None:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            await s3.copy_object(
                Bucket=self._bucket, Key=dest.path(),
                CopySource={"Bucket": self._bucket, "Key": source.path()},
            )

    # ---- multipart ----
    async def initiate_multipart(self, key, content_type) -> str:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            resp = await s3.create_multipart_upload(Bucket=self._bucket, Key=key.path(), ContentType=content_type)
            return resp["UploadId"]

    async def upload_part(self, key, upload_id, part_number, data: bytes) -> str:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            resp = await s3.upload_part(
                Bucket=self._bucket, Key=key.path(), UploadId=upload_id, PartNumber=part_number, Body=data,
            )
            return resp["ETag"].strip('"')

    async def complete_multipart(self, key, upload_id, parts: dict[int, str]) -> UploadResult:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            resp = await s3.complete_multipart_upload(
                Bucket=self._bucket, Key=key.path(), UploadId=upload_id,
                MultipartUpload={"Parts": [{"PartNumber": n, "ETag": etag} for n, etag in sorted(parts.items())]},
            )
            head = await s3.head_object(Bucket=self._bucket, Key=key.path())
            return UploadResult(key, head["ContentLength"], resp["ETag"].strip('"'),
                                 head.get("ContentType", ""), "s3")

    async def abort_multipart(self, key, upload_id) -> None:
        session = self._session()
        async with session.client("s3", region_name=self._region, endpoint_url=self._endpoint_url) as s3:
            await s3.abort_multipart_upload(Bucket=self._bucket, Key=key.path(), UploadId=upload_id)


def _to_metadata(obj: dict, tenant_id: str) -> ObjectMetadata:
    key_path = obj["Key"]
    parts = key_path[len(tenant_id) + 1:].split("/")
    document_id = parts[0] if parts else ""
    filename = parts[-1] if parts else key_path
    quarantine = len(parts) > 1 and parts[1] == "quarantine"
    return ObjectMetadata(
        key=StorageKey(tenant_id, document_id, filename, quarantine=quarantine),
        size_bytes=obj["Size"], content_type="application/octet-stream",
        etag=obj.get("ETag", "").strip('"'), last_modified=obj["LastModified"],
        storage_class=obj.get("StorageClass", "STANDARD").lower(),
    )
