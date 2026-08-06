"""FastAPI entrypoint: SSE streaming chat with a LangChain agent."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from app.agent import get_agent
from app.config import get_settings
from app.schemas import ChatRequest, MessageType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

settings = get_settings()

app = FastAPI(title="LangChain Agent Chat API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk_text(content: Any) -> str:
    """Normalize message content (str or list of content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _to_langchain_messages(messages: list[MessageType]) -> list[BaseMessage]:
    """Map OpenAI-style request messages to LangChain messages by `role`.

    Falls back to HumanMessage for unknown roles so the agent always receives
    a non-empty history.
    """
    result: list[BaseMessage] = []
    for m in messages:
        content = m.content
        if m.role == "user":
            result.append(HumanMessage(content=content))
        elif m.role == "assistant":
            result.append(AIMessage(content=content))
        elif m.role == "system":
            result.append(SystemMessage(content=content))
        else:
            result.append(HumanMessage(content=content))
    return result


async def chat_events(request: ChatRequest) -> AsyncIterator[str]:
    """Stream the agent's reply as OpenAI-style SSE chunks.

    Each `data:` line is a JSON object of shape
    `{"choices": [{"delta": {"content": "..."}}]}`, matching what
    `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses. Tool calls and
    tool results from the agent are not surfaced — the agent will emit a
    final text message after any tool loop, which is what the UI renders.

    The stream is terminated with `data: [DONE]`. If the agent raises,
    the error message is appended as a final content delta so it surfaces
    in the chat bubble instead of being silently dropped.
    """
    agent = get_agent()
    config = {"configurable": {"thread_id": request.conversation_id}}

    try:
        async for chunk, _metadata in agent.astream(
            {"messages": _to_langchain_messages(request.messages)},
            config=config,
            stream_mode="messages",
        ):
            if isinstance(chunk, AIMessageChunk):
                text = _chunk_text(chunk.content)
                if text:
                    yield sse({"choices": [{"delta": {"content": text}}]})
    except Exception as exc:  # noqa: BLE001 - surface any agent error to the client
        logger.exception("Agent stream failed")
        yield sse({"choices": [{"delta": {"content": f"\n\n[Agent 调用失败: {exc}]"}}]})

    yield "data: [DONE]\n\n"


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
    }


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        chat_events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
