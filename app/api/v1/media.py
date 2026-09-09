"""VOD and live-stream (CCTV) API. VOD: register a MediaAsset (pointing
at an already-uploaded storage object, a local-disk import path, or a
remote URL) and trigger transcoding. Live: register a source URL,
start/stop it — actual supervision happens in the separate
streaming-worker process (app/streaming_worker_main.py), this route only
flips the status flag the worker polls for.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.db.deps import get_db
from app.core.exceptions import NotFoundError, ValidationAppError
from app.core.ratelimit.limiter import rate_limit
from app.core.tenancy.context import get_tenant
from app.models.media import LiveStreamSource, MediaAsset
from app.models.user import User
from app.schemas.media import (
    LiveStreamSourceCreate,
    LiveStreamSourceOut,
    MediaAssetCreate,
    MediaAssetOut,
)
from app.services.audit_service import record as audit_record
from app.streaming.security import UnsafeStreamSourceError, validate_stream_source_url
from app.tasks.media_tasks import transcode_vod_task

router = APIRouter(prefix="/media", tags=["media"])


# ---------------- VOD ----------------

@router.get("/videos", response_model=list[MediaAssetOut])
async def list_media_assets(
    user: User = Depends(require_permission("media.read")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(MediaAsset).where(MediaAsset.tenant_id == get_tenant()).limit(100))
    return list(result.scalars().all())


@router.get("/videos/{asset_id}", response_model=MediaAssetOut)
async def get_media_asset(
    asset_id: uuid.UUID,
    user: User = Depends(require_permission("media.read")),
    db: AsyncSession = Depends(get_db),
):
    asset = await db.get(MediaAsset, asset_id)
    if asset is None or asset.tenant_id != get_tenant():
        raise NotFoundError("Media asset not found")
    return asset


@router.get("/videos/{asset_id}/playlist-url")
async def get_playback_url(
    asset_id: uuid.UUID,
    user: User = Depends(require_permission("media.read")),
    db: AsyncSession = Depends(get_db),
):
    """Returns a presigned URL to the HLS master playlist — reuses the
    exact same presigned-URL mechanism as document downloads, since HLS
    output lives in the same StorageProvider abstraction."""
    from datetime import timedelta

    from app.config import settings
    from app.storage.base import PresignedUrlPermission, StorageKey
    from app.storage.factory import get_storage_provider

    asset = await db.get(MediaAsset, asset_id)
    if asset is None or asset.tenant_id != get_tenant():
        raise NotFoundError("Media asset not found")
    if asset.status != "ready" or not asset.hls_manifest_key:
        raise ValidationAppError(f"Media asset is not ready for playback (status={asset.status})")

    provider = get_storage_provider()
    key = StorageKey(str(asset.tenant_id), str(asset.id), "hls/master.m3u8")
    presigned = await provider.generate_presigned_url(
        key, PresignedUrlPermission.READ,
        expires_in=timedelta(seconds=settings.STORAGE_PRESIGNED_URL_DEFAULT_TTL_SECONDS),
    )
    # NOTE: individual segment (.ts) and per-rendition playlist URLs also
    # need presigning for a real player to fetch them — a production
    # frontend typically proxies HLS playback through a small edge
    # handler that rewrites relative URIs in the manifest to presigned
    # equivalents, or (simpler) serves HLS output from a public CDN-backed
    # bucket with a longer-lived, scoped access policy instead of
    # per-segment presigning. Flagged here as the next integration point
    # rather than silently declared "done."
    return {"playlist_url": presigned.url, "expires_at": presigned.expires_at}


@router.post("/videos", response_model=MediaAssetOut, status_code=201,
             dependencies=[Depends(rate_limit("media.upload", 20, 60))])
async def create_media_asset(
    payload: MediaAssetCreate,
    user: User = Depends(require_permission("media.write")),
    db: AsyncSession = Depends(get_db),
):
    if payload.source_type == "remote_url":
        try:
            validate_stream_source_url(payload.source_reference)
        except UnsafeStreamSourceError as e:
            raise ValidationAppError(f"Unsafe source URL: {e}")

    asset = MediaAsset(
        tenant_id=get_tenant(), uploaded_by=user.id, filename=payload.filename,
        media_type=payload.media_type, source_type=payload.source_type,
        source_reference=payload.source_reference, status="pending",
    )
    db.add(asset)
    await db.commit()

    audit_record("media.asset.created", resource_type="media_asset", resource_id=str(asset.id),
                 metadata={"source_type": payload.source_type})
    transcode_vod_task.delay(str(asset.id), str(get_tenant()))
    return asset


# ---------------- Live / CCTV ----------------

@router.get("/live-sources", response_model=list[LiveStreamSourceOut])
async def list_live_sources(
    user: User = Depends(require_permission("media.read")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(LiveStreamSource).where(LiveStreamSource.tenant_id == get_tenant()))
    return list(result.scalars().all())


@router.post("/live-sources", response_model=LiveStreamSourceOut, status_code=201)
async def create_live_source(
    payload: LiveStreamSourceCreate,
    user: User = Depends(require_permission("media.write")),
    db: AsyncSession = Depends(get_db),
):
    try:
        validate_stream_source_url(payload.source_url)
    except UnsafeStreamSourceError as e:
        raise ValidationAppError(f"Unsafe source URL: {e}")

    source = LiveStreamSource(
        tenant_id=get_tenant(), created_by=user.id, name=payload.name,
        source_url=payload.source_url, record_to_storage=payload.record_to_storage, status="stopped",
    )
    db.add(source)
    await db.commit()
    audit_record("media.live_source.created", resource_type="live_stream_source", resource_id=str(source.id))
    return source


@router.post("/live-sources/{source_id}/start", response_model=LiveStreamSourceOut,
             dependencies=[Depends(rate_limit("media.stream.control", 10, 60))])
async def start_live_source(
    source_id: uuid.UUID,
    user: User = Depends(require_permission("media.stream.manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(LiveStreamSource, source_id)
    if source is None or source.tenant_id != get_tenant():
        raise NotFoundError("Live stream source not found")
    if source.status in ("starting", "live"):
        return source
    source.status = "starting"  # the streaming-worker process picks this up on its next poll
    await db.commit()
    audit_record("media.live_source.start_requested", resource_type="live_stream_source",
                 resource_id=str(source_id), actor_user_id=user.id)
    return source


@router.post("/live-sources/{source_id}/stop", response_model=LiveStreamSourceOut,
             dependencies=[Depends(rate_limit("media.stream.control", 10, 60))])
async def stop_live_source(
    source_id: uuid.UUID,
    user: User = Depends(require_permission("media.stream.manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(LiveStreamSource, source_id)
    if source is None or source.tenant_id != get_tenant():
        raise NotFoundError("Live stream source not found")
    if source.status in ("stopped", "stopping"):
        return source
    source.status = "stopping"
    await db.commit()
    audit_record("media.live_source.stop_requested", resource_type="live_stream_source",
                 resource_id=str(source_id), actor_user_id=user.id)
    return source


@router.get("/live-sources/{source_id}/playlist-info")
async def get_live_playlist_info(
    source_id: uuid.UUID,
    user: User = Depends(require_permission("media.read")),
    db: AsyncSession = Depends(get_db),
):
    """Live HLS output is served directly from LIVE_HLS_OUTPUT_ROOT (a
    shared volume) by a dedicated static-file route or nginx in front of
    it — NOT proxied through the storage abstraction, since round-tripping
    every ~6s live segment through cloud storage would add unacceptable
    latency for real-time viewing. This endpoint just tells the client
    where to find it."""
    source = await db.get(LiveStreamSource, source_id)
    if source is None or source.tenant_id != get_tenant():
        raise NotFoundError("Live stream source not found")
    if source.status != "live":
        raise ValidationAppError(f"Source is not currently live (status={source.status})")
    return {
        "status": source.status,
        "playlist_path": f"/api/v1/media/live-hls/{source.tenant_id}/{source.id}/playlist.m3u8",
        "last_heartbeat_at": source.last_heartbeat_at,
    }
