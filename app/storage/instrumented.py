"""Wraps any concrete StorageProvider. This is the ONLY place metrics,
audit logging, and the final tenant-scope assertion happen — every
provider implementation is wrapped here exactly once (see factory.py),
so no individual adapter can accidentally skip them."""
import time

import structlog
from prometheus_client import Counter, Histogram

from app.core.tenancy.context import get_tenant_optional
from app.storage.base import (
    InvalidStorageKeyError,
    ObjectMetadata,
    PresignedUrl,
    PresignedUrlPermission,
    StorageError,
    StorageKey,
    StorageProvider,
    UploadResult,
)

log = structlog.get_logger("storage")

storage_bytes_total = Counter(
    "storage_bytes_total", "Bytes transferred", ["tenant_id", "operation", "provider"]
)
storage_operation_latency = Histogram(
    "storage_operation_latency_seconds", "Provider call latency", ["operation", "provider"]
)
storage_operation_errors = Counter(
    "storage_operation_errors_total", "Failed storage operations", ["operation", "provider", "error_type"]
)

# Static $/GB estimates for cost reporting — updated quarterly from
# published pricing, never a live pricing API call on the hot path.
_COST_PER_GB_USD = {
    "s3": 0.023, "gcs": 0.020, "azure": 0.018, "cloudinary": 0.040, "local": 0.0,
}


def _estimate_cost_usd(size_bytes: int, provider: str) -> float:
    rate = _COST_PER_GB_USD.get(provider, 0.025)
    return round((size_bytes / (1024 ** 3)) * rate, 6)


class InstrumentedStorageProvider(StorageProvider):
    def __init__(self, inner: StorageProvider):
        self._inner = inner
        self._provider_name = inner.__class__.__name__.replace("StorageProvider", "").lower()

    def _assert_tenant_scope(self, key: StorageKey) -> None:
        current = get_tenant_optional()
        if current is not None and key.tenant_id != str(current):
            raise InvalidStorageKeyError("StorageKey tenant_id does not match request tenant context")

    def _record_audit(self, action: str, key: StorageKey, extra: dict | None = None) -> None:
        # Imported lazily to avoid a circular import (audit_service ->
        # tasks -> ... ; storage is a leaf module that shouldn't force
        # that import chain at module-load time).
        from app.services.audit_service import record as audit_record

        audit_record(action, resource_type="document", resource_id=key.document_id,
                      metadata={"provider": self._provider_name, **(extra or {})})

    async def upload(self, key, data, content_type, size_bytes=None) -> UploadResult:
        self._assert_tenant_scope(key)
        start = time.perf_counter()
        try:
            result = await self._inner.upload(key, data, content_type, size_bytes)
        except StorageError as e:
            storage_operation_errors.labels("upload", self._provider_name, type(e).__name__).inc()
            raise
        finally:
            storage_operation_latency.labels("upload", self._provider_name).observe(time.perf_counter() - start)

        storage_bytes_total.labels(key.tenant_id, "upload", self._provider_name).inc(result.size_bytes)
        self._record_audit("storage.upload", key, {
            "bytes": result.size_bytes, "content_type": content_type,
            "estimated_cost_usd": _estimate_cost_usd(result.size_bytes, self._provider_name),
            "quarantine": key.quarantine,
        })
        return result

    async def download(self, key):
        self._assert_tenant_scope(key)
        start = time.perf_counter()
        total_bytes = 0
        try:
            async for chunk in self._inner.download(key):
                total_bytes += len(chunk)
                yield chunk
        except StorageError as e:
            storage_operation_errors.labels("download", self._provider_name, type(e).__name__).inc()
            raise
        finally:
            storage_operation_latency.labels("download", self._provider_name).observe(time.perf_counter() - start)
            if total_bytes:
                storage_bytes_total.labels(key.tenant_id, "download", self._provider_name).inc(total_bytes)
                self._record_audit("storage.download", key, {"bytes": total_bytes})

    async def delete(self, key) -> None:
        self._assert_tenant_scope(key)
        start = time.perf_counter()
        try:
            await self._inner.delete(key)
        except StorageError as e:
            storage_operation_errors.labels("delete", self._provider_name, type(e).__name__).inc()
            raise
        finally:
            storage_operation_latency.labels("delete", self._provider_name).observe(time.perf_counter() - start)
        self._record_audit("storage.delete", key)

    async def generate_presigned_url(self, key, permission, expires_in=None, content_type=None) -> PresignedUrl:
        from datetime import timedelta

        self._assert_tenant_scope(key)
        if expires_in is None:
            expires_in = timedelta(minutes=15)
        result = await self._inner.generate_presigned_url(key, permission, expires_in, content_type)
        self._record_audit("storage.presigned_url.issued", key, {
            "permission": permission.value, "expires_at": result.expires_at.isoformat(),
        })
        return result

    async def list_objects(self, tenant_id, prefix="") -> list[ObjectMetadata]:
        current = get_tenant_optional()
        if current is not None and tenant_id != str(current):
            raise InvalidStorageKeyError("list_objects tenant_id does not match request tenant context")
        return await self._inner.list_objects(tenant_id, prefix)

    async def get_metadata(self, key) -> ObjectMetadata:
        self._assert_tenant_scope(key)
        return await self._inner.get_metadata(key)

    async def copy(self, source, dest) -> None:
        self._assert_tenant_scope(source)
        self._assert_tenant_scope(dest)
        await self._inner.copy(source, dest)
        self._record_audit("storage.copy", dest, {"source_quarantine": source.quarantine})

    async def initiate_multipart(self, key, content_type) -> str:
        self._assert_tenant_scope(key)
        return await self._inner.initiate_multipart(key, content_type)

    async def upload_part(self, key, upload_id, part_number, data: bytes) -> str:
        self._assert_tenant_scope(key)
        return await self._inner.upload_part(key, upload_id, part_number, data)

    async def complete_multipart(self, key, upload_id, parts: dict[int, str]) -> UploadResult:
        self._assert_tenant_scope(key)
        result = await self._inner.complete_multipart(key, upload_id, parts)
        storage_bytes_total.labels(key.tenant_id, "upload", self._provider_name).inc(result.size_bytes)
        self._record_audit("storage.upload.multipart_completed", key, {"bytes": result.size_bytes, "parts": len(parts)})
        return result

    async def abort_multipart(self, key, upload_id) -> None:
        self._assert_tenant_scope(key)
        await self._inner.abort_multipart(key, upload_id)
        self._record_audit("storage.upload.multipart_aborted", key)
