"""Dev/test only — the factory refuses to select this when ENV=production.
No real cloud presigning: issues a short-lived signed JWT-based token
verified by a local dev-only route, preserving the *shape* of the
interface (a URL you can GET/PUT without further auth) for integration
testing without needing real cloud credentials."""
import os
import shutil
from datetime import datetime, timedelta
from typing import AsyncIterator, BinaryIO

import aiofiles

from app.storage.base import (
    ObjectMetadata,
    ObjectNotFoundError,
    PresignedUrl,
    PresignedUrlPermission,
    StorageKey,
    StorageProvider,
    UploadResult,
    InvalidStorageKeyError,
)


class LocalFilesystemStorageProvider(StorageProvider):
    def __init__(self, root_dir: str, base_url: str = "http://localhost:8000/api/v1/dev-storage"):
        self._root = root_dir
        self._base_url = base_url
        os.makedirs(self._root, exist_ok=True)

    def _fs_path(self, key: StorageKey) -> str:
        path = os.path.join(self._root, key.path())
        real_root = os.path.realpath(self._root)
        real_path = os.path.realpath(path)
        if not (real_path == real_root or real_path.startswith(real_root + os.sep)):
            # Defense in depth against a StorageKey somehow carrying a
            # traversal payload past _safe_filename() — belt and suspenders.
            raise InvalidStorageKeyError("Resolved path escapes storage root")
        return path

    async def upload(self, key, data, content_type, size_bytes=None) -> UploadResult:
        path = self._fs_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        size = 0
        async with aiofiles.open(path, "wb") as f:
            if isinstance(data, (bytes, bytearray)):
                await f.write(data)
                size = len(data)
            elif hasattr(data, "read"):
                content = data.read()
                await f.write(content)
                size = len(content)
            else:
                async for chunk in data:
                    await f.write(chunk)
                    size += len(chunk)
        return UploadResult(key, size, "", content_type, "local")

    async def download(self, key) -> AsyncIterator[bytes]:
        path = self._fs_path(key)
        if not os.path.exists(path):
            raise ObjectNotFoundError(key.path())
        async with aiofiles.open(path, "rb") as f:
            while chunk := await f.read(1024 * 1024):
                yield chunk

    async def delete(self, key) -> None:
        path = self._fs_path(key)
        if os.path.exists(path):
            os.remove(path)

    async def generate_presigned_url(self, key, permission, expires_in=timedelta(minutes=15),
                                      content_type=None) -> PresignedUrl:
        import jwt

        from app.config import settings

        token = jwt.encode(
            {"path": key.path(), "perm": permission.value,
             "exp": datetime.utcnow() + expires_in},
            settings.APP_SECRET_KEY, algorithm="HS256",
        )
        method = "PUT" if permission == PresignedUrlPermission.WRITE else "GET"
        return PresignedUrl(f"{self._base_url}?token={token}", datetime.utcnow() + expires_in, method)

    async def list_objects(self, tenant_id, prefix="") -> list[ObjectMetadata]:
        base = os.path.join(self._root, tenant_id, prefix)
        results = []
        if not os.path.isdir(base):
            return results
        for dirpath, _, filenames in os.walk(base):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, self._root)
                parts = rel.split(os.sep)
                if len(parts) < 3:
                    continue
                tid, doc_id, *rest = parts
                key = StorageKey(tid, doc_id, rest[-1], quarantine=(rest[0] == "quarantine" if len(rest) > 1 else False))
                stat = os.stat(full)
                results.append(ObjectMetadata(
                    key=key, size_bytes=stat.st_size, content_type="application/octet-stream",
                    etag=None, last_modified=datetime.fromtimestamp(stat.st_mtime),
                ))
        return results

    async def get_metadata(self, key) -> ObjectMetadata:
        path = self._fs_path(key)
        if not os.path.exists(path):
            raise ObjectNotFoundError(key.path())
        stat = os.stat(path)
        return ObjectMetadata(key=key, size_bytes=stat.st_size, content_type="application/octet-stream",
                               etag=None, last_modified=datetime.fromtimestamp(stat.st_mtime))

    async def copy(self, source, dest) -> None:
        src_path = self._fs_path(source)
        dst_path = self._fs_path(dest)
        if not os.path.exists(src_path):
            raise ObjectNotFoundError(source.path())
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copyfile(src_path, dst_path)

    # ---- multipart (buffer to a temp dir, single upload on complete — fine for dev/test) ----
    async def initiate_multipart(self, key, content_type) -> str:
        import uuid
        upload_id = str(uuid.uuid4())
        os.makedirs(self._multipart_dir(upload_id), exist_ok=True)
        return upload_id

    async def upload_part(self, key, upload_id, part_number, data: bytes) -> str:
        part_path = os.path.join(self._multipart_dir(upload_id), f"part_{part_number:05d}")
        async with aiofiles.open(part_path, "wb") as f:
            await f.write(data)
        import hashlib
        return hashlib.md5(data).hexdigest()

    async def complete_multipart(self, key, upload_id, parts: dict[int, str]) -> UploadResult:
        final_path = self._fs_path(key)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        size = 0
        with open(final_path, "wb") as out:
            for part_number in sorted(parts.keys()):
                part_path = os.path.join(self._multipart_dir(upload_id), f"part_{part_number:05d}")
                with open(part_path, "rb") as pf:
                    chunk = pf.read()
                    out.write(chunk)
                    size += len(chunk)
        shutil.rmtree(self._multipart_dir(upload_id), ignore_errors=True)
        return UploadResult(key, size, "", "application/octet-stream", "local")

    async def abort_multipart(self, key, upload_id) -> None:
        shutil.rmtree(self._multipart_dir(upload_id), ignore_errors=True)

    def _multipart_dir(self, upload_id: str) -> str:
        return os.path.join(self._root, "_multipart", upload_id)
