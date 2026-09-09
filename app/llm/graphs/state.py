from typing import TypedDict


class ChatState(TypedDict):
    tenant_id: str
    conversation_id: str
    messages: list[dict]          # [{"role": "user"|"assistant", "content": str}, ...]
    retrieved_context: list[str]
    model_override: str | None
