import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    # 8000 chars is a deliberate ceiling: generous for real usage, but
    # bounded so a single request can't be used to run up unbounded LLM
    # token cost or attempt a prompt-injection payload of unlimited size.
    message: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=100)  # must exist in the provider registry


class ChatChunkOut(BaseModel):
    type: str  # "token" | "tool_call" | "done" | "error"
    content: str


class IngestResponse(BaseModel):
    job_id: str
