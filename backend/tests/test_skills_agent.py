"""技能接入 agent 的集成测试: 目录注入、工具收敛、服务端鉴权、SSE 思维链。

两层验证:
1. 用 tmp_path 技能目录 + 可编程 mock 模型, 精确断言 middleware 与工具的行为;
2. 用仓库内置技能走 /api/chat SSE, 验证前端思维链能看到技能加载过程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agent import MockChatModel
from app.context import ChatContext
from app.middleware.agent_skills import SkillMiddleware
from app.skills import SkillRegistry
from app.skills.tools import SKILL_TOOL_NAMES, SKILL_TOOLS
from app.tools import ALL_TOOLS
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from skills_helpers import KNOWN_TOOLS, write_skill

BASE_PROMPT = "BASE_SYSTEM_PROMPT"

SEEN_PROMPTS: list[str] = []
SEEN_TOOLS: list[list[str]] = []


class RecordingModel(MockChatModel):
    """复用 mock 的流式行为, 同时记录模型真正收到的系统提示与工具集。"""

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        SEEN_TOOLS.append([t.name for t in tools])
        return self

    def _next_message(self, messages):
        for message in messages:
            if isinstance(message, SystemMessage):
                SEEN_PROMPTS.append(str(message.content))
        return super()._next_message(messages)


class ScriptedModel(MockChatModel):
    """固定发起一次指定工具调用, 用于精确驱动 agent 循环(而非依赖关键词猜测)。"""

    call: dict | None = None

    def _next_message(self, messages):
        if self.call is not None and messages[-1].type != "tool":
            return AIMessage(
                content="",
                tool_calls=[{**self.call, "id": "scripted-call", "type": "tool_call"}],
            )
        return super()._next_message(messages)


@pytest.fixture(autouse=True)
def _reset_records():
    SEEN_PROMPTS.clear()
    SEEN_TOOLS.clear()
    yield
    SEEN_PROMPTS.clear()
    SEEN_TOOLS.clear()


@pytest.fixture
def registry(tmp_path, monkeypatch) -> SkillRegistry:
    """两个技能: 公开 trip-demo + 需要 skill:payroll:use 的 payroll; 并让工具指向它。"""
    write_skill(tmp_path, "trip-demo", description="规划一日行程。", body="# 行程步骤\n1. 查天气\n")
    write_skill(
        tmp_path,
        "payroll",
        description="计算工资。",
        visibility="permission",
        requires_permissions=["skill:payroll:use"],
        body="# 工资步骤\n1. 算税\n",
        extra_files={"references/rules.md": "个税规则"},
    )
    reg = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS)
    reg.reload()
    monkeypatch.setattr("app.skills.tools.get_skill_registry", lambda: reg)
    return reg


def build_agent(reg: SkillRegistry, model=None):
    """与 app/agent.py 相同的接线方式, 但用传入的 registry 且不挂 checkpointer。"""
    return create_agent(
        model=model or RecordingModel(),
        tools=[*ALL_TOOLS, *SKILL_TOOLS],
        system_prompt=BASE_PROMPT,
        middleware=[SkillMiddleware(reg)],
        context_schema=ChatContext,
    )


async def run_agent(agent, *, context: ChatContext, text: str = "你好"):
    return await agent.ainvoke({"messages": [HumanMessage(content=text)]}, context=context)


def tool_messages(result) -> list[str]:
    return [str(m.content) for m in result["messages"] if m.type == "tool"]


ANONYMOUS = ChatContext()
MEMBER = ChatContext(account_id=7, authenticated=True, permissions=frozenset({"chat:send"}))
PAYROLL_USER = ChatContext(
    account_id=9, authenticated=True, permissions=frozenset({"skill:payroll:use"})
)
ADMIN = ChatContext(account_id=1, authenticated=True, permissions=frozenset({"*"}))


# --------------------------------------------------------------------------- L1 目录注入
async def test_catalog_is_appended_to_system_prompt(registry):
    agent = build_agent(registry)
    await run_agent(agent, context=ANONYMOUS)
    assert SEEN_PROMPTS, "模型未收到系统提示"
    assert all(BASE_PROMPT in prompt for prompt in SEEN_PROMPTS)
    assert any("trip-demo" in prompt and "load_skill" in prompt for prompt in SEEN_PROMPTS)


async def test_anonymous_catalog_hides_gated_skill(registry):
    agent = build_agent(registry)
    await run_agent(agent, context=ANONYMOUS)
    assert not any("payroll" in prompt for prompt in SEEN_PROMPTS)


@pytest.mark.parametrize(
    "context",
    [PAYROLL_USER, ADMIN],
    ids=["explicit-permission", "wildcard-permission"],
)
async def test_gated_skill_visible_with_permission(registry, context):
    agent = build_agent(registry)
    await run_agent(agent, context=context)
    assert any("payroll" in prompt for prompt in SEEN_PROMPTS)


async def test_logged_in_user_sees_auth_level_skills(tmp_path, monkeypatch):
    write_skill(tmp_path, "internal-note", visibility="auth")
    reg = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS)
    reg.reload()
    monkeypatch.setattr("app.skills.tools.get_skill_registry", lambda: reg)
    agent = build_agent(reg)

    await run_agent(agent, context=ANONYMOUS)
    assert not any("internal-note" in prompt for prompt in SEEN_PROMPTS)

    SEEN_PROMPTS.clear()
    await run_agent(agent, context=MEMBER)
    assert any("internal-note" in prompt for prompt in SEEN_PROMPTS)


# --------------------------------------------------------------------------- 工具收敛
async def test_skill_tools_are_exposed_when_skills_visible(registry):
    agent = build_agent(registry)
    await run_agent(agent, context=ANONYMOUS)
    assert any(SKILL_TOOL_NAMES <= set(names) for names in SEEN_TOOLS)


async def test_skill_tools_are_hidden_when_nothing_visible(tmp_path, monkeypatch):
    write_skill(
        tmp_path,
        "payroll-only",
        visibility="permission",
        requires_permissions=["skill:payroll:use"],
    )
    reg = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS)
    reg.reload()
    monkeypatch.setattr("app.skills.tools.get_skill_registry", lambda: reg)
    agent = build_agent(reg)

    await run_agent(agent, context=ANONYMOUS)
    assert SEEN_TOOLS, "模型未被调用"
    assert all(not (SKILL_TOOL_NAMES & set(names)) for names in SEEN_TOOLS)
    # 通用工具不受技能可见性影响
    assert all("get_current_time" in names for names in SEEN_TOOLS)


async def test_disabled_registry_hides_catalog_and_tools(tmp_path):
    write_skill(tmp_path, "trip-demo")
    reg = SkillRegistry(tmp_path, enabled=False, known_tools=KNOWN_TOOLS)
    reg.reload()
    agent = build_agent(reg)

    await run_agent(agent, context=ADMIN)
    assert all("trip-demo" not in prompt for prompt in SEEN_PROMPTS)
    assert all(not (SKILL_TOOL_NAMES & set(names)) for names in SEEN_TOOLS)


# --------------------------------------------------------------------------- L2/L3 工具行为
async def test_load_skill_returns_wrapped_body(registry):
    agent = build_agent(
        registry, model=ScriptedModel(call={"name": "load_skill", "args": {"name": "trip-demo"}})
    )
    result = await run_agent(agent, context=ANONYMOUS)
    body = tool_messages(result)[0]
    assert '<skill name="trip-demo"' in body
    assert "行程步骤" in body


async def test_load_skill_denied_without_permission(registry):
    agent = build_agent(
        registry, model=ScriptedModel(call={"name": "load_skill", "args": {"name": "payroll"}})
    )
    result = await run_agent(agent, context=MEMBER)
    assert "技能不可用" in tool_messages(result)[0]


async def test_load_skill_denied_for_unknown_skill(registry):
    agent = build_agent(
        registry, model=ScriptedModel(call={"name": "load_skill", "args": {"name": "ghost"}})
    )
    result = await run_agent(agent, context=ADMIN)
    assert "技能不可用" in tool_messages(result)[0]


async def test_read_skill_file_returns_attachment_for_authorized_user(registry):
    agent = build_agent(
        registry,
        model=ScriptedModel(
            call={"name": "read_skill_file", "args": {"name": "payroll", "path": "references/rules.md"}}
        ),
    )
    result = await run_agent(agent, context=PAYROLL_USER)
    assert "个税规则" in tool_messages(result)[0]


async def test_read_skill_file_blocks_traversal(registry):
    agent = build_agent(
        registry,
        model=ScriptedModel(
            call={"name": "read_skill_file", "args": {"name": "trip-demo", "path": "../payroll/SKILL.md"}}
        ),
    )
    result = await run_agent(agent, context=ADMIN)
    assert "无法读取技能附件" in tool_messages(result)[0]


async def test_middleware_blocks_skill_tools_when_none_visible(tmp_path, monkeypatch):
    """模型幻觉调用技能工具时, middleware 兜底拦截(工具实现都不会被执行)。"""
    write_skill(
        tmp_path,
        "payroll-only",
        visibility="permission",
        requires_permissions=["skill:payroll:use"],
    )
    reg = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS)
    reg.reload()
    monkeypatch.setattr("app.skills.tools.get_skill_registry", lambda: reg)
    agent = build_agent(
        reg, model=ScriptedModel(call={"name": "load_skill", "args": {"name": "payroll-only"}})
    )

    result = await run_agent(agent, context=ANONYMOUS)
    assert "没有可用技能" in tool_messages(result)[0]


async def test_list_skills_reports_visible_names(registry):
    agent = build_agent(registry, model=ScriptedModel(call={"name": "list_skills", "args": {}}))
    result = await run_agent(agent, context=PAYROLL_USER)
    listed = tool_messages(result)[0]
    assert "trip-demo" in listed
    assert "payroll" in listed


# --------------------------------------------------------------------------- 真实接线
async def test_singleton_agent_registers_skill_tools_and_accepts_context():
    """app/agent.py 的单例 agent 必须挂上技能工具、SkillMiddleware 与 ChatContext。

    走真实接线(mock 模型 + 内置技能): 若 SKILL_TOOLS 未注册或 context_schema 缺失,
    这里会分别表现为「没有 load_skill 事件」与「ainvoke 拒绝 context 关键字」。
    """
    from app.agent import close_checkpointer, get_agent

    try:
        agent = await get_agent()
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="你有什么技能")]},
            config={"configurable": {"thread_id": "skills-wiring"}},
            context=ADMIN,
        )
    finally:
        # 单例 agent 绑定了 sqlite checkpointer(内部 asyncio.Lock 记住本用例的事件循环),
        # 必须在同一个循环里释放, 否则后面的 TestClient 用例会撞上跨循环复用报错。
        await close_checkpointer()

    names = [c["name"] for m in result["messages"] if m.type == "ai" for c in (m.tool_calls or [])]
    assert "load_skill" in names, names
    bodies = tool_messages(result)
    assert bodies and "<skill name=" in bodies[0]


def test_chat_sse_exposes_skill_loading_in_thought_chain():
    """匿名 mock 模式下问「你有什么技能」, SSE 应带出 load_skill 的调用与结果。"""
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "你有什么技能"}],
                "conversation_id": "skill-smoke",
            },
        )
    assert response.status_code == 200
    events = []
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line[len("data: ") :])
        if "agent" in payload:
            events.append(payload["agent"])

    calls = [e for e in events if e["type"] == "tool_call" and e["name"] == "load_skill"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert calls, f"未看到 load_skill 调用: {events}"
    assert calls[0]["args"]["name"]
    assert any("<skill name=" in e["result"] for e in results), results
    assert "服务暂时不可用" not in response.text


def test_bundled_skills_dir_is_configured_absolute():
    """conftest 把 SKILLS_DIR 固定为仓库内置技能目录, 用例不应随工作目录漂移。"""
    from app.config import get_settings

    configured = Path(get_settings().skills_dir)
    assert configured.is_absolute()
    assert (configured / "trip-planner" / "SKILL.md").is_file()
