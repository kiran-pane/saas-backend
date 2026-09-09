"""Document upload API: direct (small-file) upload, multipart/resumable
(large-file) upload, and presigned-download retrieval. Every path funnels
through the same quarantine -> virus scan -> commit -> ingest pipeline
(app/tasks/storage_tasks.py) — there is no upload path that skips
scanning, including multipart completion.
"""
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission
from app.config import settings
from app.core.db.deps import get_db
from app.core.exceptions import NotFoundError, ValidationAppError
from app.core.ratelimit.limiter import rate_limit
from app.core.tenancy.context import get_tenant
from app.models.llm import Document
from app.models.user import User
from app.schemas.storage import (
    DocumentOut,
    MultipartCompleteResponse,
    MultipartInitRequest,
    MultipartInitResponse,
    MultipartStatusResponse,
    PresignedDownloadResponse,
)
from app.services.audit_service import record as audit_record
from app.storage.base import PresignedUrlPermission, StorageKey
from app.storage.factory import get_storage_provider
from app.storage.multipart import MultipartSessionNotFoundError, MultipartUploadCoordinator
from app.tasks.storage_tasks import scan_document_task

router = APIRouter(prefix="/documents", tags=["documents"])
_multipart = MultipartUploadCoordinator()


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    user: User = Depends(require_permission("documents.read")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Document).where(Document.tenant_id == get_tenant()).limit(100))
    return list(result.scalars().all())


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    user: User = Depends(require_permission("documents.read")),
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.tenant_id != get_tenant():
        raise NotFoundError("Document not found")
    return document


@router.get("/{document_id}/download-url", response_model=PresignedDownloadResponse)
async def get_download_url(
    document_id: uuid.UUID,
    user: User = Depends(require_permission("documents.read")),
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.tenant_id != get_tenant():
        raise NotFoundError("Document not found")
    if document.status not in ("ready", "indexed"):
        raise ValidationAppError(f"Document is not available for download (status={document.status})")

    provider = get_storage_provider()
    key = StorageKey(str(document.tenant_id), str(document.id), document.filename)
    presigned = await provider.generate_presigned_url(
        key, PresignedUrlPermission.READ,
        expires_in=timedelta(seconds=settings.STORAGE_PRESIGNED_URL_DEFAULT_TTL_SECONDS),
    )
    return PresignedDownloadResponse(
        document_id=document.id, filename=document.filename,
        download_url=presigned.url, expires_at=presigned.expires_at,
    )


@router.post("/upload", response_model=DocumentOut, status_code=201,
             dependencies=[Depends(rate_limit("documents.upload", 20, 60))])
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form(default="markdown"),
    user: User = Depends(require_permission("documents.write")),
    db: AsyncSession = Depends(get_db),
):
    """Direct (small-file, single-shot) upload. Files above
    STORAGE_MAX_UPLOAD_BYTES should use the multipart flow instead — this
    endpoint enforces that cap rather than silently buffering an
    arbitrarily large file into memory."""
    contents = await file.read(settings.STORAGE_MAX_UPLOAD_BYTES + 1)
    if len(contents) > settings.STORAGE_MAX_UPLOAD_BYTES:
        raise ValidationAppError(
            f"File exceeds the {settings.STORAGE_MAX_UPLOAD_BYTES} byte single-shot upload limit; "
            "use the multipart upload flow instead"
        )

    document = Document(
        tenant_id=get_tenant(), uploaded_by=user.id, filename=file.filename or "unnamed",
        doc_type=doc_type, status="pending_upload", storage_provider=settings.STORAGE_PROVIDER,
        content_type=file.content_type,
    )
    db.add(document)
    await db.flush()

    provider = get_storage_provider()
    quarantine_key = StorageKey(str(get_tenant()), str(document.id), document.filename, quarantine=True)
    await provider.upload(quarantine_key, contents, file.content_type or "application/octet-stream")

    document.status = "scanning"
    await db.commit()

    audit_record("document.upload.received", resource_type="document", resource_id=str(document.id),
                 metadata={"bytes": len(contents), "filename": document.filename})
    scan_document_task.delay(str(document.id), str(get_tenant()))
    return document


@router.post("/uploads/multipart", response_model=MultipartInitResponse,
             dependencies=[Depends(rate_limit("documents.upload", 20, 60))])
async def init_multipart_upload(
    payload: MultipartInitRequest,
    user: User = Depends(require_permission("documents.write")),
    db: AsyncSession = Depends(get_db),
):
    document = Document(
        tenant_id=get_tenant(), uploaded_by=user.id, filename=payload.filename,
        doc_type=payload.doc_type, status="pending_upload", storage_provider=settings.STORAGE_PROVIDER,
        content_type=payload.content_type, size_bytes=payload.total_size,
    )
    db.add(document)
    await db.flush()
    await db.commit()

    resume_token = await _multipart.start(
        tenant_id=str(get_tenant()), document_id=str(document.id), filename=payload.filename,
        total_size=payload.total_size, content_type=payload.content_type,
    )
    audit_record("document.upload.multipart_started", resource_type="document", resource_id=str(document.id),
                 metadata={"total_size": payload.total_size})
    return MultipartInitResponse(document_id=document.id, resume_token=resume_token, part_size=8 * 1024 * 1024)


@router.put("/uploads/multipart/{resume_token}/parts/{part_number}", status_code=204)
async def upload_multipart_part(
    resume_token: str,
    part_number: int,
    file: UploadFile = File(...),
    user: User = Depends(require_permission("documents.write")),
):
    data = await file.read()
    try:
        await _multipart.upload_part(resume_token, part_number, data)
    except MultipartSessionNotFoundError:
        raise NotFoundError("Multipart upload session not found or expired")


@router.get("/uploads/multipart/{resume_token}", response_model=MultipartStatusResponse)
async def get_multipart_status(resume_token: str, user: User = Depends(get_current_user)):
    try:
        completed = await _multipart.get_resume_state(resume_token)
    except MultipartSessionNotFoundError:
        raise NotFoundError("Multipart upload session not found or expired")
    return MultipartStatusResponse(completed_parts=completed)


@router.post("/uploads/multipart/{resume_token}/complete", response_model=MultipartCompleteResponse)
async def complete_multipart_upload(
    resume_token: str,
    user: User = Depends(require_permission("documents.write")),
    db: AsyncSession = Depends(get_db),
):
    try:
        session = await _multipart.get_session(resume_token)
    except MultipartSessionNotFoundError:
        raise NotFoundError("Multipart upload session not found or expired")

    document = await db.get(Document, uuid.UUID(session.document_id))
    if document is None or document.tenant_id != get_tenant():
        raise NotFoundError("Document not found")

    try:
        result = await _multipart.complete(resume_token)
    except MultipartSessionNotFoundError:
        raise NotFoundError("Multipart upload session not found or expired")

    document.status = "scanning"
    document.size_bytes = result.size_bytes
    await db.commit()

    audit_record("document.upload.multipart_completed", resource_type="document", resource_id=str(document.id),
                 metadata={"bytes": result.size_bytes})
    scan_document_task.delay(str(document.id), str(get_tenant()))
    return MultipartCompleteResponse(document_id=document.id, size_bytes=result.size_bytes, status="scanning")


@router.delete("/uploads/multipart/{resume_token}", status_code=204)
async def abort_multipart_upload(resume_token: str, user: User = Depends(require_permission("documents.write"))):
    try:
        await _multipart.abort(resume_token)
    except MultipartSessionNotFoundError:
        pass  # idempotent — already gone is not an error
