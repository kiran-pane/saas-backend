"""Entrypoint for the dedicated live-streaming supervisor process.

Run as its OWN container (see docker-compose.yml's `streaming-worker`
service) — deliberately NOT a Celery task and NOT part of the FastAPI
process, because supervising long-running ffmpeg subprocesses for
CCTV/RTSP feeds is a fundamentally different operational shape than
either request/response HTTP handling or one-shot background jobs:
    - it must run continuously, indefinitely, for as long as any tenant
      has a live source active
    - it owns live child processes (ffmpeg) that must be cleanly
      terminated on shutdown, not abandoned
    - a crash/restart of this process should resume supervising existing
      "live" sources rather than losing track of them (poll_and_reconcile
      re-derives state from the database on every pass, not from
      in-memory state alone, precisely so a restart is safe)

Usage: `python -m app.streaming_worker_main`
"""
import asyncio
import signal

import structlog

from app.config import settings
from app.core.logging.setup import configure_logging
from app.streaming.live_manager import LiveStreamManager

log = structlog.get_logger("streaming_worker")


async def main() -> None:
    configure_logging()
    manager = LiveStreamManager()
    stop_event = asyncio.Event()

    def _handle_shutdown_signal():
        log.info("streaming_worker_shutdown_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_shutdown_signal)

    log.info("streaming_worker_started", poll_interval=settings.LIVE_STREAM_POLL_INTERVAL_SECONDS)

    while not stop_event.is_set():
        try:
            await manager.poll_and_reconcile()
        except Exception:
            log.exception("streaming_worker_reconcile_error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.LIVE_STREAM_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass  # normal — just means the poll interval elapsed

    log.info("streaming_worker_stopping_all_active_streams")
    for source_id, process in list(manager._processes.items()):  # noqa: SLF001 — shutdown path, own module
        process.terminate()
    await asyncio.sleep(2)
    for process in manager._processes.values():  # noqa: SLF001
        if process.returncode is None:
            process.kill()

    log.info("streaming_worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
