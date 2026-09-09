"""LangGraph state graph for multi-turn chat with retrieval + moderation.

Use LangGraph (rather than a single prompt call) whenever a request needs
multiple steps, tool calls, or branching. State is persisted via a Postgres
checkpointer (see checkpointer.py) — never in-process memory — so
conversations survive process restarts and work across horizontally
scaled API replicas.
"""
from langgraph.graph import END, StateGraph

from app.llm.graphs.state import ChatState
from app.llm.providers.registry import get_model
from app.llm.tracing import build_run_tags


async def retrieve_context_node(state: ChatState) -> ChatState:
    # Placeholder: wire to app/llm/ingestion/pipeline.py's hybrid retriever
    # (pgvector cosine + Postgres full-text, reciprocal-rank-fused).
    # Left as an explicit TODO hook rather than a fake implementation.
    return {**state, "retrieved_context": []}


async def generate_node(state: ChatState) -> ChatState:
    model = get_model(primary=state.get("model_override"))
    context_block = "\n".join(state["retrieved_context"])
    system = f"You are a helpful assistant. Relevant context:\n{context_block}" if context_block else \
        "You are a helpful assistant."

    lc_messages = [{"role": "system", "content": system}] + state["messages"]
    response = await model.ainvoke(
        lc_messages,
        config={"tags": build_run_tags(["node:generate"])},
    )
    new_messages = state["messages"] + [{"role": "assistant", "content": response.content}]
    return {**state, "messages": new_messages}


async def moderation_node(state: ChatState) -> ChatState:
    # Placeholder for a moderation/safety pass on the generated content
    # before it's returned to the client. Kept as its own node so it can
    # be swapped, disabled per-tenant, or expanded independently.
    return state


def build_chat_graph(checkpointer):
    graph = StateGraph(ChatState)
    graph.add_node("retrieve", retrieve_context_node)
    graph.add_node("generate", generate_node)
    graph.add_node("moderate", moderation_node)
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "moderate")
    graph.add_edge("moderate", END)
    return graph.compile(checkpointer=checkpointer)
