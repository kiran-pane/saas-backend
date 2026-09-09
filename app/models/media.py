import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TenantScopedMixin, TimestampMixin, new_uuid


class MediaAsset(Base, TimestampMixin, TenantScopedMixin):
    """Video-on-demand (VOD) or image asset. source_type determines where
    the ORIGINAL file comes from before transcoding; the transcoded HLS
    output always lands in the tenant's configured StorageProvider
    (reusing the existing storage abstraction — no new storage code for
    VOD output)."""
    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), default="video")  # video|image
    # source_type: local_disk | storage (already-uploaded via app.storage) | remote_url (fetched once, then treated as local)
    source_type: Mapped[str] = mapped_column(String(20), default="storage")
    source_reference: Mapped[str] = mapped_column(String(2000), nullable=False)  # path / StorageKey path / URL
    # pending -> transcoding -> ready | failed | rejected_invalid_type | rejected_virus
    status: Mapped[str] = mapped_column(String(30), default="pending")
    hls_manifest_key: Mapped[str | None] = mapped_column(String(1000))  # StorageKey path to master.m3u8
    thumbnail_key: Mapped[str | None] = mapped_column(String(1000))
    duration_seconds: Mapped[float | None] = mapped_column()
    size_bytes: Mapped[int | None] = mapped_column()
    renditions: Mapped[dict] = mapped_column(JSON, default=dict)  # {"720p": {...}, "480p": {...}}
    failure_reason: Mapped[str | None] = mapped_column(String(1000))


class LiveStreamSource(Base, TimestampMixin, TenantScopedMixin):
    """A CCTV camera or other live RTSP/RTMP feed. Registered once,
    started/stopped independently of the registration itself — a
    dedicated streaming-worker process (NOT the request/response API
    process, and NOT a one-shot Celery task) supervises the actual
    ffmpeg process for each active source, since live transcoding is a
    long-lived operation fundamentally different from a VOD batch job."""
    __tablename__ = "live_stream_sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Validated against app/streaming/security.py's SSRF guard at
    # creation time AND re-validated immediately before each stream
    # start (DNS can change between registration and start).
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)  # rtsp://... or rtmp://...
    # stopped -> starting -> live -> stopping | error
    status: Mapped[str] = mapped_column(String(20), default="stopped")
    hls_output_path: Mapped[str | None] = mapped_column(String(1000))  # local path served by the API/nginx
    last_heartbeat_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(1000))
    restart_count: Mapped[int] = mapped_column(default=0)
    record_to_storage: Mapped[bool] = mapped_column(default=False)  # optional DVR-style archival, see live_manager.py
