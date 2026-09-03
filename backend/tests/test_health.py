"""健康/探针端点与 LLM 超时配置冒烟。"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def client():
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def test_live_always_ok(client):
    """存活探针不触碰依赖, 必须恒 200。"""
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_unhealthy_dependencies(client):
    """conftest 将 MySQL/Redis 指向死端口 → 就绪探针必须 503 并指明依赖。"""
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == 50000
    failed = body["data"]
    assert any(item.startswith("mysql:") for item in failed)
    assert any(item.startswith("redis:") for item in failed)


def test_llm_timeout_default_and_override():
    from app.config import Settings

    assert Settings(_env_file=None, app_env="dev").llm_timeout_seconds == 60.0
    s = Settings(_env_file=None, app_env="dev", llm_timeout_seconds=5.5)
    assert s.llm_timeout_seconds == 5.5


def test_build_chat_model_applies_timeout():
    """超时配置必须真正落到 LLM 客户端, 否则上游挂死会拖住 SSE。"""
    from app.agent import build_chat_model
    from app.config import Settings

    s = Settings(
        _env_file=None,
        app_env="dev",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_timeout_seconds=7.5,
    )
    model = build_chat_model(s)
    assert model.request_timeout == 7.5


def test_build_chat_model_applies_timeout_anthropic():
    from app.agent import build_chat_model
    from app.config import Settings

    s = Settings(
        _env_file=None,
        app_env="dev",
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
        llm_timeout_seconds=7.5,
    )
    model = build_chat_model(s)
    assert model.default_request_timeout == 7.5
