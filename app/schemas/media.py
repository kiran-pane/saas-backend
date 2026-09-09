import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class MediaAssetCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    source_type: str = Field(default="storage", max_length=20)  # storage | local_disk | remote_url
    source_reference: str = Field(min_length=1, max_length=2000)
    media_type: str = Field(default="video", max_length=20)

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, v: str) -> str:
        if v not in ("storage", "local_disk", "remote_url"):
            raise ValueError("source_type must be one of: storage, local_disk, remote_url")
        return v


class MediaAssetOut(BaseModel):
    id: uuid.UUID
    filename: str
    media_type: str
    status: str
    hls_manifest_key: str | None
    duration_seconds: float | None
    renditions: dict
    failure_reason: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class LiveStreamSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=1, max_length=2000)
    record_to_storage: bool = False


class LiveStreamSourceOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    hls_output_path: str | None
    last_heartbeat_at: datetime | None
    last_error: str | None
    restart_count: int

    class Config:
        from_attributes = True
