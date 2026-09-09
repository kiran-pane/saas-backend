import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user, require_permission
from app.core.ratelimit.limiter import rate_limit
from app.core.tenancy.context import get_tenant
from app.llm.graphs.chat_graph import build_chat_graph
from app.llm.graphs.checkpointer import get_checkpointer
from app.llm.tracing import tracing_context
from app.models.user import User
from app.schemas.llm import ChatRequest, IngestResponse
from app.tasks.llm_tasks import ingest_document_task

router = APIRouter(prefix="/llm", tags=["llm"])


@router.post("/chat", dependencies=[Depends(rate_limit("llm.chat", 30, 60))])
async def chat(req: ChatRequest, user: User = Depends(get_current_user)):
    checkpointer = await get_checkpointer()
    graph = build_chat_graph(checkpointer)

    async def event_stream():
        with tracing_context():
            async for event in graph.astream(
                {
                    "tenant_id": str(get_tenant()),
                    "conversation_id": req.conversation_id,
                    "messages": [{"role": "user", "content": req.message}],
                    "retrieved_context": [],
                    "model_override": req.model,
                },
                config={"configurable": {"thread_id": req.conversation_id}},
            ):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/documents/{document_id}/ingest", response_model=IngestResponse)
async def ingest_document(
    document_id: str,
    user: User = Depends(require_permission("documents.write")),
):
    task = ingest_document_task.delay(document_id, str(get_tenant()))
    return IngestResponse(job_id=task.id)


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, user: User = Depends(get_current_user)):
    from app.tasks.celery_app import celery_app

    result = celery_app.AsyncResult(job_id)
    return {"job_id": job_id, "status": result.status, "result": result.result if result.ready() else None}
