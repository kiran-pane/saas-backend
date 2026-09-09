"""Supervises long-running ffmpeg processes for live/CCTV sources. This
runs inside the dedicated streaming-worker process (see
app/streaming_worker_main.py), NOT inside the FastAPI request/response
cycle and NOT as a one-shot Celery task — a live camera feed needs a
process that lives for hours/days and restarts itself on crash, which is
a different operational shape than either of those.

Coordination between the API (start/stop requests) and this worker
happens through the database: the API flips LiveStreamSource.status to
"starting"/"stopping", and this manager polls for status changes on a
short interval. Simple and durable (survives worker restarts) rather
than a more complex message-passing scheme — appropriate given the
typically small number of concurrent live sources per deployment.
"""
import asyncio
import os

import structlog
from sqlalchemy import select

from app.config import settings
from app.core.db.session import system_session_factory
from app.models.media import LiveStreamSource
from app.streaming.ffmpeg_runner import build_live_transcode_command, start_ffmpeg_process
from app.streaming.hls import DEFAULT_LIVE_RENDITION_LADDER
from app.streaming.security import UnsafeStreamSourceError, validate_stream_source_url

log = structlog.get_logger("live_stream_manager")


class LiveStreamManager:
    def __init__(self):
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _output_dir(self, tenant_id: str, source_id: str) -> str:
        return os.path.join(settings.LIVE_HLS_OUTPUT_ROOT, tenant_id, source_id)

    async def poll_and_reconcile(self) -> None:
        """Single reconciliation pass: start sources marked "starting",
        stop sources marked "stopping", restart any that died
        unexpectedly while marked "live". Called on a loop from
        streaming_worker_main.py."""
        async with system_session_factory() as db:
            result = await db.execute(select(LiveStreamSource))
            sources = result.scalars().all()

            for source in sources:
                sid = str(source.id)
                if source.status == "starting" and sid not in self._processes:
                    await self._start(db, source)
                elif source.status == "stopping" and sid in self._processes:
                    await self._stop(db, source)
                elif source.status == "live" and sid not in self._processes:
                    # Process died between reconciliation passes (crash,
                    # OOM-kill, etc.) — restart with backoff tracked via
                    # restart_count, don't restart-loop forever on a
                    # permanently broken camera.
                    if source.restart_count < settings.LIVE_STREAM_MAX_RESTARTS:
                        log.warning("live_stream_unexpected_exit_restarting", source_id=sid)
                        await self._start(db, source, is_restart=True)
                    else:
                        source.status = "error"
                        source.last_error = "Exceeded max restart attempts"
                        await db.commit()
                        log.error("live_stream_max_restarts_exceeded", source_id=sid)
                elif source.status == "live" and sid in self._processes:
                    process = self._processes[sid]
                    if process.returncode is not None:
                        # Process object still tracked but has actually
                        # exited — same handling as "died between passes"
                        # above; clean up the stale reference first.
                        del self._processes[sid]

            # Clean up processes for sources that no longer exist in the DB at all.
            known_ids = {str(s.id) for s in sources}
            for stale_id in list(self._processes.keys()):
                if stale_id not in known_ids:
                    self._processes[stale_id].kill()
                    del self._processes[stale_id]

    async def _start(self, db, source: LiveStreamSource, is_restart: bool = False) -> None:
        sid = str(source.id)
        try:
            # Re-validate immediately before connecting — DNS can change
            # between registration and start (DNS rebinding protection).
            tenant_allows_private = await self._tenant_allows_private_networks(db, source.tenant_id)
            validate_stream_source_url(source.source_url, allow_private_networks=tenant_allows_private)
        except UnsafeStreamSourceError as e:
            source.status = "error"
            source.last_error = f"Source URL failed validation: {e}"
            await db.commit()
            log.error("live_stream_unsafe_source", source_id=sid, error=str(e))
            return

        output_dir = self._output_dir(str(source.tenant_id), sid)
        record_path = None
        if source.record_to_storage:
            record_path = os.path.join(settings.LIVE_RECORDING_TEMP_ROOT, str(source.tenant_id), sid,
                                        "recording_%Y%m%d_%H%M%S.mp4")

        cmd = build_live_transcode_command(source.source_url, output_dir, DEFAULT_LIVE_RENDITION_LADDER,
                                            record_to_path=record_path)
        process = await start_ffmpeg_process(cmd)
        self._processes[sid] = process
        source.status = "live"
        source.hls_output_path = output_dir
        source.last_heartbeat_at = _now()
        if is_restart:
            source.restart_count += 1
        else:
            source.restart_count = 0
        source.last_error = None
        await db.commit()
        log.info("live_stream_started", source_id=sid, restart=is_restart)

        self._tasks[sid] = asyncio.create_task(self._watch_stderr(sid, process))

    async def _stop(self, db, source: LiveStreamSource) -> None:
        sid = str(source.id)
        process = self._processes.get(sid)
        if process is not None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
            del self._processes[sid]
        if sid in self._tasks:
            self._tasks[sid].cancel()
            del self._tasks[sid]
        source.status = "stopped"
        await db.commit()
        log.info("live_stream_stopped", source_id=sid)

    async def _watch_stderr(self, source_id: str, process: asyncio.subprocess.Process) -> None:
        """Captures the tail of ffmpeg's stderr for the last_error field
        if the process exits unexpectedly — without this, a crashed
        camera feed gives no diagnostic beyond "it stopped"."""
        tail_lines: list[str] = []
        try:
            async for line in process.stderr:
                decoded = line.decode(errors="replace").strip()
                if decoded:
                    tail_lines.append(decoded)
                    tail_lines = tail_lines[-20:]
        except asyncio.CancelledError:
            return

        if process.returncode not in (0, None, -15):  # -15 = SIGTERM, an intentional stop
            async with system_session_factory() as db:
                result = await db.execute(select(LiveStreamSource).where(LiveStreamSource.id == source_id))
                source = result.scalar_one_or_none()
                if source and source.status == "live":
                    source.last_error = "\n".join(tail_lines)[-1000:]
                    await db.commit()

    async def _tenant_allows_private_networks(self, db, tenant_id) -> bool:
        from app.models.tenant import Tenant
        result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
        return bool(tenant and (tenant.settings or {}).get("allow_private_stream_sources", False))


def _now():
    from datetime import datetime
    return datetime.utcnow()
