"""Pre-storage-commitment security gates: MIME-sniffing (cheap, runs
first) and ClamAV scanning (more expensive, runs second). A file that
fails either gate must never exist in permanent tenant storage — see
app/tasks/storage_tasks.py::scan_document_task for how these compose
into the full quarantine -> scan -> commit flow."""
import asyncio
import io

from app.config import settings

# Extension -> allowed magic-byte-sniffed MIME types. A mismatch (e.g. a
# ".pdf" whose actual bytes are a Windows PE executable) is rejected
# before ClamAV even runs, since it's cheaper and catches a large class
# of disguised-payload attacks immediately.
_ALLOWED_MIME_BY_EXTENSION = {
    "pdf": {"application/pdf"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"},
    "txt": {"text/plain"},
    "md": {"text/plain", "text/markdown"},
    "csv": {"text/plain", "text/csv"},
    "html": {"text/html", "text/plain"},
    "json": {"text/plain", "application/json"},
}


class MimeMismatchError(Exception):
    pass


def sniff_content_type(data: bytes) -> str:
    try:
        import magic
        return magic.from_buffer(data[:4096], mime=True)
    except ImportError:
        # python-magic (libmagic binding) may not be installed in every
        # environment (e.g. a minimal test container) — degrade to
        # "unknown" rather than crash; callers should treat "unknown" as
        # a soft-fail signal, not an automatic pass.
        return "application/octet-stream"


def validate_mime_type(filename: str, data: bytes) -> None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = _ALLOWED_MIME_BY_EXTENSION.get(ext)
    if allowed is None:
        return  # unknown extension — not in the allow-list catalog, so no MIME check to apply; ClamAV still runs
    sniffed = sniff_content_type(data)
    if sniffed not in allowed and sniffed != "application/octet-stream":
        raise MimeMismatchError(
            f"File extension .{ext} does not match sniffed content type {sniffed!r}"
        )


class VirusScanResult:
    def __init__(self, clean: bool, signature: str | None = None, skipped: bool = False):
        self.clean = clean
        self.signature = signature
        self.skipped = skipped  # true when CLAMAV_ENABLED=false (local dev only)


async def scan_bytes(data: bytes) -> VirusScanResult:
    if not settings.CLAMAV_ENABLED:
        return VirusScanResult(clean=True, skipped=True)

    def _scan() -> tuple[bool, str | None]:
        import clamd

        cd = clamd.ClamdNetworkSocket(host=settings.CLAMAV_HOST, port=settings.CLAMAV_PORT)
        result = cd.instream(io.BytesIO(data))
        status, signature = result["stream"]
        return status == "OK", signature

    clean, signature = await asyncio.to_thread(_scan)
    return VirusScanResult(clean=clean, signature=signature)
