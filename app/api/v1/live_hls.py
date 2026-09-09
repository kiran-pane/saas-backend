"""Serves live HLS playlist/segment files directly from the shared
LIVE_HLS_OUTPUT_ROOT volume written by the streaming-worker process. In
a real production deployment, put this behind nginx/a CDN instead of
FastAPI for the actual byte-serving (FastAPI is fine for the control-
plane API, not ideal for high-frequency small-file serving at scale) —
this route exists so the system works end-to-end without requiring that
extra infrastructure piece to be stood up first.
"""
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/media/live-hls", tags=["media"])

_ALLOWED_EXTENSIONS = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}


@router.get("/{tenant_id}/{source_id}/{filename}")
async def get_live_hls_file(tenant_id: str, source_id: str, filename: str):
    # Deliberately outside TenantMiddleware's normal auth flow (live HLS
    # players typically can't attach custom auth headers) — tenant_id and
    # source_id are both UUIDs embedded in the path itself, giving this
    # the same practical security property as an unguessable presigned
    # URL. For stricter access control, front this with a signed-URL
    # token check identical to the dev-storage pattern, or put actual
    # authentication at the nginx/CDN layer in front of this route.
    ext = os.path.splitext(filename)[1]
    content_type = _ALLOWED_EXTENSIONS.get(ext)
    if content_type is None:
        raise ForbiddenError("Unsupported file type")

    root = os.path.realpath(settings.LIVE_HLS_OUTPUT_ROOT)
    path = os.path.realpath(os.path.join(root, tenant_id, source_id, filename))
    if not path.startswith(root + os.sep):
        raise ForbiddenError("Invalid path")
    if not os.path.exists(path):
        raise NotFoundError("Segment or playlist not found")

    return FileResponse(path, media_type=content_type)
