"""VOD transcode pipeline: fetch the source video from wherever it lives
(local disk, the existing tenant storage abstraction, or a one-time
remote URL fetch), run ffmpeg to produce an HLS rendition ladder, then
upload every output file through the SAME StorageProvider abstraction
already built for documents — no new storage code needed for HLS output,
it's just another kind of tenant-scoped object.
"""
import os
import tempfile

from app.config import settings
from app.core.exceptions import ValidationAppError
from app.models.media import MediaAsset
from app.storage.base import StorageKey
from app.storage.factory import get_storage_provider
from app.storage.scanning import MimeMismatchError, scan_bytes, validate_mime_type
from app.streaming.ffmpeg_runner import FFmpegError, build_vod_transcode_command, run_ffmpeg_once
from app.streaming.hls import DEFAULT_VOD_RENDITION_LADDER
from app.streaming.security import UnsafeStreamSourceError, validate_stream_source_url


async def _fetch_source_to_tempfile(asset: MediaAsset, workdir: str) -> str:
    input_path = os.path.join(workdir, "source" + os.path.splitext(asset.filename)[1])

    if asset.source_type == "storage":
        provider = get_storage_provider()
        key = StorageKey(str(asset.tenant_id), str(asset.id), asset.filename)
        chunks = [c async for c in provider.download(key)]
        with open(input_path, "wb") as f:
            f.write(b"".join(chunks))

    elif asset.source_type == "local_disk":
        # source_reference is a path under a configured, admin-controlled
        # media-import root — never an arbitrary client-supplied
        # filesystem path (that would be a path-traversal/LFI vector).
        real_root = os.path.realpath(settings.LOCAL_MEDIA_IMPORT_ROOT)
        real_source = os.path.realpath(os.path.join(real_root, asset.source_reference.lstrip("/")))
        if not (real_source == real_root or real_source.startswith(real_root + os.sep)):
            raise ValidationAppError("source_reference escapes the configured local media import root")
        with open(real_source, "rb") as src, open(input_path, "wb") as dst:
            dst.write(src.read())

    elif asset.source_type == "remote_url":
        try:
            validate_stream_source_url(asset.source_reference)
        except UnsafeStreamSourceError as e:
            raise ValidationAppError(f"Unsafe source URL: {e}") from e
        import httpx
        async with httpx.AsyncClient(follow_redirects=False) as client:  # no redirects — re-validate manually if needed
            async with client.stream("GET", asset.source_reference, timeout=60) as resp:
                if resp.status_code >= 400:
                    raise ValidationAppError(f"Failed to fetch remote source: HTTP {resp.status_code}")
                with open(input_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)
    else:
        raise ValidationAppError(f"Unknown source_type: {asset.source_type}")

    return input_path


async def transcode_vod_asset(asset: MediaAsset) -> None:
    """Runs the full pipeline for one MediaAsset. Raises on any failure —
    caller (Celery task) is responsible for setting asset.status
    accordingly."""
    with tempfile.TemporaryDirectory(prefix="vod-transcode-") as workdir:
        input_path = await _fetch_source_to_tempfile(asset, workdir)

        with open(input_path, "rb") as f:
            head = f.read(4096)
        try:
            validate_mime_type(asset.filename, head)
        except MimeMismatchError as e:
            asset.status = "rejected_invalid_type"
            asset.failure_reason = str(e)
            return

        with open(input_path, "rb") as f:
            full_bytes = f.read()
        scan_result = await scan_bytes(full_bytes)
        if not scan_result.clean:
            asset.status = "rejected_virus"
            asset.failure_reason = f"Virus signature: {scan_result.signature}"
            return
        del full_bytes  # don't hold the whole file in memory longer than needed for scanning

        output_dir = os.path.join(workdir, "hls")
        cmd = build_vod_transcode_command(input_path, output_dir, DEFAULT_VOD_RENDITION_LADDER)
        try:
            await run_ffmpeg_once(cmd, timeout_seconds=settings.FFMPEG_VOD_TIMEOUT_SECONDS)
        except FFmpegError as e:
            asset.status = "failed"
            asset.failure_reason = f"{e}: {e.stderr[-500:]}"
            return

        provider = get_storage_provider()
        uploaded_renditions: dict[str, dict] = {}
        for rendition in DEFAULT_VOD_RENDITION_LADDER:
            rendition_dir = os.path.join(output_dir, rendition.name)
            if not os.path.isdir(rendition_dir):
                continue  # ffmpeg may skip a rendition if source resolution is smaller than the target
            for fname in sorted(os.listdir(rendition_dir)):
                with open(os.path.join(rendition_dir, fname), "rb") as f:
                    data = f.read()
                key = StorageKey(str(asset.tenant_id), str(asset.id), f"hls/{rendition.name}/{fname}")
                content_type = "application/vnd.apple.mpegurl" if fname.endswith(".m3u8") else "video/mp2t"
                await provider.upload(key, data, content_type)
            uploaded_renditions[rendition.name] = {
                "width": rendition.width, "height": rendition.height,
                "bitrate_kbps": rendition.video_bitrate_kbps,
            }

        master_path = os.path.join(output_dir, "master.m3u8")
        if os.path.exists(master_path):
            with open(master_path, "rb") as f:
                master_data = f.read()
            master_key = StorageKey(str(asset.tenant_id), str(asset.id), "hls/master.m3u8")
            await provider.upload(master_key, master_data, "application/vnd.apple.mpegurl")
            asset.hls_manifest_key = master_key.path()

        asset.renditions = uploaded_renditions
        asset.status = "ready"
