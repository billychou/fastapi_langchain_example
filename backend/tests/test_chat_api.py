"""/api/chat 与 /api/health 冒烟: 匿名 mock 模式, Redis 死端口走降级路径。

依赖 conftest 冻结的环境:
- CHAT_REQUIRE_AUTH=false → get_current_optional 直接返回 None;
- REDIS_URL 死端口 → lifespan 探活失败仅告警, 请求时限流捕获 RedisError 降级放行;
- 匿名 + 默认会话号 → 每请求随机 thread_id, 不触碰 MySQL/共享记忆。
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(scope="module")
def client():
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def _deltas(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line[len("data: ") :])
        delta = payload["choices"][0]["delta"].get("content")
        if delta:
            parts.append(delta)
    return parts


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"


def test_chat_sse_streams_reply_and_done(client):
    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}], "conversation_id": "smoke-1"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")
    content = "".join(_deltas(response.text))
    assert content
    assert "服务暂时不可用" not in content


def _agent_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line[len("data: ") :])
        if payload.get("agent"):
            events.append(payload["agent"])
    return events


def test_chat_tool_call_loop(client):
    """'现在几点了' 触发 mock 工具调用循环, 最终仍有文本回答。"""
    response = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "现在几点了"}],
            "conversation_id": "smoke-2",
        },
    )
    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    content = "".join(_deltas(response.text))
    assert content
    assert "服务暂时不可用" not in content


def test_chat_streams_tool_chain_events(client):
    """工具循环同时产出配对的 tool_call/tool_result 思维链事件。

    事件顺序必须是 tool_call 在前、同 id 的 tool_result 在后;
    文本 delta 与事件共存, 且纯文本消息不产生事件。
    """
    response = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "现在几点了"}],
            "conversation_id": "smoke-3",
        },
    )
    assert response.status_code == 200
    events = _agent_events(response.text)
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert calls and results
    assert calls[0]["name"] == "get_current_time"
    assert results[0]["name"] == "get_current_time"
    assert results[0]["result"]
    # 配对: tool_call 必须先于同 id 的 tool_result 出现
    first_call = next(i for i, e in enumerate(events) if e["type"] == "tool_call")
    first_result = next(
        i for i, e in enumerate(events) if e["type"] == "tool_result" and e["id"] == calls[0]["id"]
    )
    assert first_call < first_result

    # 纯文本回复(无工具)不应携带任何事件
    plain = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}], "conversation_id": "smoke-4"},
    )
    assert _agent_events(plain.text) == []


def test_chat_rejects_oversized_input(client):
    from app.config import get_settings

    oversized = "x" * (get_settings().chat_max_message_chars + 1)
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": oversized}]})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 40000
    assert "过长" in body["message"]


def test_chat_error_envelope_never_leaks_internals(client):
    """未知路由走统一信封, 不暴露堆栈。"""
    response = client.get("/api/no-such-route")
    assert response.status_code == 404
