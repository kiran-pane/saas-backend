import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    status: str
    size_bytes: int | None
    content_type: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class MultipartInitRequest(BaseModel):
    filename: str = Field(max_length=500)
    doc_type: str = Field(default="markdown", max_length=50)
    content_type: str = Field(max_length=150)
    total_size: int = Field(gt=0)


class MultipartInitResponse(BaseModel):
    document_id: uuid.UUID
    resume_token: str
    part_size: int


class MultipartStatusResponse(BaseModel):
    completed_parts: list[int]


class MultipartCompleteResponse(BaseModel):
    document_id: uuid.UUID
    size_bytes: int
    status: str


class PresignedDownloadResponse(BaseModel):
    document_id: uuid.UUID
    filename: str
    download_url: str
    expires_at: datetime
