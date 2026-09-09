"""Multipart/resumable upload session state, persisted in Redis (fast,
TTL-native — a stalled upload should expire, not linger forever). A
client that loses connection mid-upload reconnects with the same
resume_token and queries which parts already succeeded instead of
re-uploading the whole file from scratch."""
import uuid
from dataclasses import asdict, dataclass, field

import orjson

from app.core.cache.redis_client import get_redis
from app.storage.base import StorageKey, UploadResult
from app.storage.factory import get_storage_provider

_SESSION_KEY_PREFIX = "multipart:"
_DEFAULT_SESSION_TTL_SECONDS = 6 * 3600  # abandoned uploads self-expire in 6h


class MultipartSessionNotFoundError(Exception):
    pass


@dataclass
class MultipartUploadSession:
    upload_id: str
    tenant_id: str
    document_id: str
    filename: str
    total_size: int
    content_type: str
    part_size: int = 8 * 1024 * 1024  # 8MB parts
    completed_parts: dict[int, str] = field(default_factory=dict)
    status: str = "in_progress"  # in_progress|completed|aborted

    def key(self) -> StorageKey:
        return StorageKey(self.tenant_id, self.document_id, self.filename)

    def to_json(self) -> bytes:
        return orjson.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: bytes) -> "MultipartUploadSession":
        data = orjson.loads(raw)
        data["completed_parts"] = {int(k): v for k, v in data.get("completed_parts", {}).items()}
        return cls(**data)


class MultipartUploadCoordinator:
    async def start(self, tenant_id: str, document_id: str, filename: str, total_size: int,
                     content_type: str) -> str:
        provider = get_storage_provider()
        key = StorageKey(tenant_id, document_id, filename)
        upload_id = await provider.initiate_multipart(key, content_type)

        resume_token = str(uuid.uuid4())
        session = MultipartUploadSession(
            upload_id=upload_id, tenant_id=tenant_id, document_id=document_id, filename=filename,
            total_size=total_size, content_type=content_type,
        )
        await self._save(resume_token, session)
        return resume_token

    async def upload_part(self, resume_token: str, part_number: int, data: bytes) -> None:
        session = await self._load(resume_token)
        provider = get_storage_provider()
        etag = await provider.upload_part(session.key(), session.upload_id, part_number, data)
        session.completed_parts[part_number] = etag
        await self._save(resume_token, session)

    async def get_resume_state(self, resume_token: str) -> list[int]:
        session = await self._load(resume_token)
        return sorted(session.completed_parts.keys())

    async def get_session(self, resume_token: str) -> MultipartUploadSession:
        return await self._load(resume_token)

    async def complete(self, resume_token: str) -> UploadResult:
        session = await self._load(resume_token)
        provider = get_storage_provider()
        result = await provider.complete_multipart(session.key(), session.upload_id, session.completed_parts)
        session.status = "completed"
        await self._save(resume_token, session)
        return result

    async def abort(self, resume_token: str) -> None:
        session = await self._load(resume_token)
        provider = get_storage_provider()
        await provider.abort_multipart(session.key(), session.upload_id)
        session.status = "aborted"
        await self._save(resume_token, session)

    async def _save(self, resume_token: str, session: MultipartUploadSession) -> None:
        redis = await get_redis()
        await redis.setex(_SESSION_KEY_PREFIX + resume_token, _DEFAULT_SESSION_TTL_SECONDS, session.to_json())

    async def _load(self, resume_token: str) -> MultipartUploadSession:
        redis = await get_redis()
        raw = await redis.get(_SESSION_KEY_PREFIX + resume_token)
        if raw is None:
            raise MultipartSessionNotFoundError(resume_token)
        return MultipartUploadSession.from_json(raw if isinstance(raw, bytes) else raw.encode())
