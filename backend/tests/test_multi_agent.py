"""多智能体运行时: 注册表 -> 总管/执行子图 -> handoff 调度 -> SSE 调度事件。

覆盖点(与 ``docs/aiops-platform-design.md`` 的调度时序一一对应):

1. **注册表驱动构造**: 只有 active 的执行智能体编译成子图并拿到 handoff 工具;
   paused 的(种子里的 edu-agent)既无子图也无 ``call_<key>`` 工具 —— 启停即时生效;
2. **调度链**: 总管 mock 按关键词把「集群里有哪些 pod」交给 k8s-agent, 子图结论经
   ToolMessage 回传, 总管不复述(already_streamed); ``AgentEventSink`` 收到
   dispatch -> return(success) 一对事件;
3. **SSE 接线**: 匿名 HTTP 流里 ``agent`` 字段带 agent_dispatch / agent_return,
   执行智能体正文经 custom lane 恰好出现一次(不重复、不丢);
4. **教学频道搬家**: pedagogy 频道写在 edu-agent 子图的派生线程
   ``f"{thread_id}::edu-agent"`` 上, 第二轮读回载体继续推进(Teach -> Detect)。

全部走 MockChatModel + sqlite checkpointer, 零外部依赖。
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import aiosqlite
import pytest
from app.agent import LEARNING_AGENT_KEY, build_runtime, invalidate_runtime
from app.agent_events import AgentEventSink
from app.agent_registry import DEFAULT_AGENT_SPECS, AgentSpec
from app.context import ChatContext
from app.middleware.agent_pedagogy import STATE_CHANNEL
from app.pedagogy.session import prepare_turn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

POD_QUESTION = "集群里有哪些 pod"
POD_NAME = "checkout-6d4f7c8b9d-x2k4p"
OPENING = "我想搞懂闭包"
EXPLANATION = (
    "函数把创建它时的那层作用域记住了，"
    "所以哪怕它后来被传到别处调用，也照样读得到当时那些变量，等于随身带着环境。"
) * 2


def specs_with_edu(status: str) -> tuple[AgentSpec, ...]:
    return tuple(
        dataclasses.replace(spec, status=status)
        if spec.agent_key == LEARNING_AGENT_KEY
        else spec
        for spec in DEFAULT_AGENT_SPECS
    )


@pytest.fixture()
async def checkpointer(tmp_path) -> Iterator[AsyncSqliteSaver]:
    conn = await aiosqlite.connect(tmp_path / "ma-checkpoints.sqlite")
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    yield saver
    await conn.close()


def frames_of(text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


# --------------------------------------------------------------------- 构造


async def test_runtime_builds_only_active_executors(checkpointer: AsyncSqliteSaver):
    """种子里 edu-agent 是 paused: 无子图、无 handoff; 改成 active 后两者都有。"""
    runtime = await build_runtime(DEFAULT_AGENT_SPECS, checkpointer)
    assert sorted(runtime.executors) == ["k8s-agent", "log-agent", "metric-agent", "rag-agent"]
    assert "call_k8s-agent" in runtime.handoff_tool_names
    assert f"call_{LEARNING_AGENT_KEY}" not in runtime.handoff_tool_names
    assert runtime.executor(LEARNING_AGENT_KEY) is None

    activated = await build_runtime(specs_with_edu("active"), checkpointer)
    assert f"call_{LEARNING_AGENT_KEY}" in activated.handoff_tool_names
    assert activated.executor(LEARNING_AGENT_KEY) is not None


async def test_supervisor_keeps_its_own_tools_alongside_handoffs(
    checkpointer: AsyncSqliteSaver,
):
    """总管 = 自身 tool_refs + 每个 active 执行智能体一个 handoff(+ 技能工具)。"""
    runtime = await build_runtime(DEFAULT_AGENT_SPECS, checkpointer)
    names = set(runtime.handoff_tool_names)
    assert names == {f"call_{key}" for key in runtime.executors}
    # 直连「几点」仍走总管自己的 get_current_time, 不发生调度。
    sink = AgentEventSink()
    ctx = ChatContext(thread_id="ma-own", agent_sink=sink)
    result = await runtime.supervisor.ainvoke(
        {"messages": [HumanMessage(content="现在几点了")]},
        config={"configurable": {"thread_id": "ma-own"}},
        context=ctx,
    )
    tool_names = [m.name for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_names == ["get_current_time"]
    assert sink.drain() == []


# --------------------------------------------------------------------- 调度链


async def test_supervisor_dispatches_to_executor_and_sink_records(
    checkpointer: AsyncSqliteSaver,
):
    runtime = await build_runtime(DEFAULT_AGENT_SPECS, checkpointer)
    sink = AgentEventSink()
    ctx = ChatContext(thread_id="ma-dispatch", agent_sink=sink)
    result = await runtime.supervisor.ainvoke(
        {"messages": [HumanMessage(content=POD_QUESTION)]},
        config={"configurable": {"thread_id": "ma-dispatch"}},
        context=ctx,
    )

    handoff_results = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "call_k8s-agent"
    ]
    assert len(handoff_results) == 1
    assert POD_NAME in str(handoff_results[0].content)
    assert handoff_results[0].additional_kwargs.get("already_streamed") is True
    # 全文已流给客户端: mock 总管收尾消息为空, 不复述。
    assert result["messages"][-1].content == ""
    assert isinstance(result["messages"][-1], AIMessage)

    events = sink.drain()
    assert [event.type for event in events] == ["agent_dispatch", "agent_return"]
    dispatch, back = events
    assert dispatch.agent_key == "k8s-agent"
    assert dispatch.name == "K8s 集群诊断"
    assert dispatch.task == POD_QUESTION
    assert back.agent_key == "k8s-agent"
    assert back.status == "success"


async def test_executor_failure_degrades_to_hint(checkpointer: AsyncSqliteSaver):
    """子图异常只降级为一句提示: 事件照发(status=error), 总管整轮不失败。"""
    specs = list(specs_with_edu("active"))
    runtime = await build_runtime(tuple(specs), checkpointer)
    sink = AgentEventSink()
    ctx = ChatContext(thread_id="ma-error", agent_sink=sink)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("subgraph down")
        yield  # 使函数成为 async generator, 与 astream 签名一致


    edu = runtime.executor(LEARNING_AGENT_KEY)
    original = edu.astream
    edu.astream = _boom  # type: ignore[method-assign]
    try:
        result = await runtime.supervisor.ainvoke(
            {"messages": [HumanMessage(content=OPENING)]},
            config={"configurable": {"thread_id": "ma-error"}},
            context=ctx,
        )
    finally:
        edu.astream = original  # type: ignore[method-assign]

    events = sink.drain()
    assert [event.type for event in events] == ["agent_dispatch", "agent_return"]
    assert events[1].status == "error"
    handoff_results = [
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == f"call_{LEARNING_AGENT_KEY}"
    ]
    assert len(handoff_results) == 1
    assert "执行失败" in str(handoff_results[0].content)


# --------------------------------------------------------------------- SSE 接线


def test_sse_stream_carries_dispatch_events_and_executor_text_once():
    """匿名 HTTP: agent_dispatch/agent_return 帧 + 执行智能体正文恰好一份。"""
    invalidate_runtime()  # 别的模块可能种过 sqlite 注册表: 本用例要内置默认(edu paused)
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/api/chat", json={"messages": [{"role": "user", "content": POD_QUESTION}]}
        )
    assert response.status_code == 200
    frames = frames_of(response.text)

    agent_events = [
        frame["agent"]
        for frame in frames
        if frame.get("agent") and frame["agent"].get("type") in ("agent_dispatch", "agent_return")
    ]
    assert [event["type"] for event in agent_events] == ["agent_dispatch", "agent_return"]
    assert agent_events[0]["agent_key"] == "k8s-agent"
    assert agent_events[0]["task"] == POD_QUESTION
    assert agent_events[1]["status"] == "success"
    # 调度帧要先于执行智能体的第一个 token: 前端思维链节点先立起来再打字。
    first_dispatch_index = next(
        index for index, frame in enumerate(frames) if frame.get("agent") == agent_events[0]
    )
    content = "".join(
        frame["choices"][0]["delta"].get("content") or "" for frame in frames if frame.get("choices")
    )
    assert POD_NAME in content
    assert content.count(POD_NAME) == 1, "执行智能体正文重复下发(父图 messages lane 泄漏)"
    first_content_index = next(
        index
        for index, frame in enumerate(frames)
        if frame.get("choices") and frame["choices"][0]["delta"].get("content")
    )
    assert first_dispatch_index < first_content_index


# --------------------------------------------------------------------- 教学频道


async def test_edu_pedagogy_channel_lives_on_derived_thread(checkpointer: AsyncSqliteSaver):
    """频道写在 ``f"{thread_id}::edu-agent"``: 两轮 invoke 后载体续得上。"""
    runtime = await build_runtime(specs_with_edu("active"), checkpointer)
    edu = runtime.executor(LEARNING_AGENT_KEY)
    thread_id = "ma-edu"
    derived = f"{thread_id}::{LEARNING_AGENT_KEY}"

    turn = prepare_turn(raw_channel=None, thread_id=thread_id, account_id=7, text=OPENING)
    assert turn is not None
    ctx = ChatContext(account_id=7, thread_id=thread_id, **turn.context_kwargs)
    await edu.ainvoke(
        {"messages": [HumanMessage(content=OPENING)]},
        config={"configurable": {"thread_id": derived}},
        context=ctx,
    )
    snapshot = await edu.aget_state({"configurable": {"thread_id": derived}})
    channel = (snapshot.values or {}).get(STATE_CHANNEL)
    assert channel is not None, "pedagogy 频道没写进派生线程"

    # 第二轮: 自述里不含概念名, 概念只能来自跨轮载体 -> 状态推进到 Detect。
    turn2 = prepare_turn(raw_channel=channel, thread_id=thread_id, account_id=7, text=EXPLANATION)
    assert turn2 is not None
    assert turn2.state.concept_id == "闭包"
    ctx2 = ChatContext(account_id=7, thread_id=thread_id, **turn2.context_kwargs)
    await edu.ainvoke(
        {"messages": [HumanMessage(content=EXPLANATION)]},
        config={"configurable": {"thread_id": derived}},
        context=ctx2,
    )
    snapshot2 = await edu.aget_state({"configurable": {"thread_id": derived}})
    channel2 = (snapshot2.values or {}).get(STATE_CHANNEL)
    assert channel2 is not None
    assert channel2 != channel
    turn3 = prepare_turn(raw_channel=channel2, thread_id=thread_id, account_id=7, text="继续")
    assert turn3 is not None
    assert turn3.state.state != turn.state.state
