"""Provider-agnostic storage interface. Nothing above this module (routes,
services, Celery tasks) may import a cloud SDK directly — everything goes
through StorageProvider, obtained via app.storage.factory.get_storage_provider().
"""
import abc
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import AsyncIterator, BinaryIO


class StorageError(Exception):
    """Base class — callers catch this, never provider-specific SDK exceptions."""


class ObjectNotFoundError(StorageError):
    pass


class StorageQuotaExceededError(StorageError):
    pass


class InvalidStorageKeyError(StorageError):
    """Raised if a key is malformed or fails tenant-scope validation."""


class PresignedUrlPermission(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class StorageKey:
    """The ONLY way to address an object. tenant_id is mandatory — there is
    no provider method that accepts a bare string path, so it is
    structurally impossible to call upload()/download() unscoped."""

    tenant_id: str
    document_id: str
    filename: str
    quarantine: bool = False  # True while awaiting virus scan (section 7 of the design doc)

    def path(self) -> str:
        prefix = "quarantine/" if self.quarantine else ""
        return f"{self.tenant_id}/{self.document_id}/{prefix}{self._safe_filename()}"

    def _safe_filename(self) -> str:
        # Strip path traversal, null bytes, and anything outside a safe
        # charset — a malicious filename like "../../etc/passwd" or
        # "doc\x00.pdf.exe" must never reach a provider SDK call.
        name = self.filename.replace("\x00", "")
        name = name.replace("/", "_").replace("\\", "_")
        name = re.sub(r"\.\.+", ".", name)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        return name[:255] or "unnamed"

    def as_final(self) -> "StorageKey":
        """Returns the non-quarantine version of this key (post-scan destination)."""
        return StorageKey(self.tenant_id, self.document_id, self.filename, quarantine=False)


@dataclass
class ObjectMetadata:
    key: StorageKey
    size_bytes: int
    content_type: str
    etag: str | None
    last_modified: datetime
    storage_class: str = "standard"  # standard|cold|archive


@dataclass
class UploadResult:
    key: StorageKey
    size_bytes: int
    etag: str
    content_type: str
    provider: str


@dataclass
class PresignedUrl:
    url: str
    expires_at: datetime
    method: str  # "GET" | "PUT" | "POST"
    extra_fields: dict = field(default_factory=dict)  # for providers needing extra form fields (e.g. Cloudinary)


# Hard caps enforced regardless of what a caller requests — see the design
# doc's presigned-URL security-considerations section.
MAX_PRESIGNED_WRITE_TTL = timedelta(hours=1)
MAX_PRESIGNED_READ_TTL = timedelta(hours=24)


class StorageProvider(abc.ABC):
    """Every method takes/returns provider-agnostic types only. Every
    concrete implementation must translate its own SDK's exceptions into
    the StorageError hierarchy above."""

    @abc.abstractmethod
    async def upload(
        self,
        key: StorageKey,
        data: BinaryIO | AsyncIterator[bytes] | bytes,
        content_type: str,
        size_bytes: int | None = None,
    ) -> UploadResult:
        ...

    @abc.abstractmethod
    async def download(self, key: StorageKey) -> AsyncIterator[bytes]:
        """Streams — callers must never assume the whole file fits in memory."""
        ...

    @abc.abstractmethod
    async def delete(self, key: StorageKey) -> None:
        """Idempotent: deleting a non-existent key is not an error."""
        ...

    @abc.abstractmethod
    async def generate_presigned_url(
        self,
        key: StorageKey,
        permission: PresignedUrlPermission,
        expires_in: timedelta = timedelta(minutes=15),
        content_type: str | None = None,
    ) -> PresignedUrl:
        ...

    @abc.abstractmethod
    async def list_objects(self, tenant_id: str, prefix: str = "") -> list[ObjectMetadata]:
        """Always scoped to a tenant_id — there is no "list everything" method."""
        ...

    @abc.abstractmethod
    async def get_metadata(self, key: StorageKey) -> ObjectMetadata:
        ...

    @abc.abstractmethod
    async def copy(self, source: StorageKey, dest: StorageKey) -> None:
        """Needed for post-scan quarantine->final moves and lifecycle transitions."""
        ...

    # ---- Multipart upload (large files) ----
    async def initiate_multipart(self, key: StorageKey, content_type: str) -> str:
        raise NotImplementedError(f"{self.__class__.__name__} does not support multipart upload")

    async def upload_part(self, key: StorageKey, upload_id: str, part_number: int, data: bytes) -> str:
        raise NotImplementedError(f"{self.__class__.__name__} does not support multipart upload")

    async def complete_multipart(
        self, key: StorageKey, upload_id: str, parts: dict[int, str]
    ) -> UploadResult:
        raise NotImplementedError(f"{self.__class__.__name__} does not support multipart upload")

    async def abort_multipart(self, key: StorageKey, upload_id: str) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} does not support multipart upload")
