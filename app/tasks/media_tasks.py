"""VOD transcode task — runs on its own 'media' queue (separate from
llm_heavy) since ffmpeg transcoding is CPU-bound in a fundamentally
different way than LLM ingestion (long-running, multi-core, no external
API calls) — isolating it means a burst of video uploads can't starve
document ingestion or vice versa, same isolation rationale as every
other queue split in this system."""
import asyncio
import uuid

from sqlalchemy import select, text

from app.core.db.session import async_session_factory
from app.tasks.celery_app import celery_app


@celery_app.task(bind=True, max_retries=1, default_retry_delay=30, soft_time_limit=3600, time_limit=3660)
def transcode_vod_task(self, media_asset_id: str, tenant_id: str):
    asyncio.run(_transcode_vod_async(media_asset_id, tenant_id))


async def _transcode_vod_async(media_asset_id: str, tenant_id: str) -> None:
    from app.models.media import MediaAsset
    from app.services.audit_service import record as audit_record
    from app.streaming.vod_pipeline import transcode_vod_asset

    async with async_session_factory() as db:
        await db.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": tenant_id})
        result = await db.execute(select(MediaAsset).where(MediaAsset.id == uuid.UUID(media_asset_id)))
        asset = result.scalar_one_or_none()
        if asset is None:
            return

        asset.status = "transcoding"
        await db.commit()

        try:
            await transcode_vod_asset(asset)
        except Exception as e:
            asset.status = "failed"
            asset.failure_reason = str(e)[:1000]
            await db.commit()
            audit_record("media.transcode.failed", resource_type="media_asset",
                         resource_id=media_asset_id, metadata={"error": str(e)[:500]})
            raise
        else:
            await db.commit()
            audit_record(
                "media.transcode." + ("completed" if asset.status == "ready" else asset.status),
                resource_type="media_asset", resource_id=media_asset_id,
                metadata={"status": asset.status},
            )
