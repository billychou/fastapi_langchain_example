"""/api/chat 的 ``learn`` SSE 通道: 真实 HTTP 上的教学接线。

前面几个 ``test_pedagogy_*.py`` 把内核当纯函数穷举; 这里测的是**接线** —— 装配(路由)
-> 注入(中间件) -> 事件(SSE) -> 跨轮(checkpointer 频道)。跑的是真 ``app`` +
真 ``create_agent`` + ``MockChatModel``, 所以只有走 HTTP 才碰得到的环节都被覆盖:
``dependency_overrides``、限流降级、会话建档、SSE 帧形状与到达顺序。

五组断言:

1. **状态先行**: 状态卡在第一个 token 增量**之前**下发(学习者先看到「现在在哪一步」,
   而不是等流结束才补一张卡);
2. **跨轮真的续得上**: 同一个 ``conversation_id`` 上, 第二句只交自述、不再提概念名,
   内核照样知道概念是「闭包」并把 ``Teach`` 推进到 ``Detect`` —— 载体存在 checkpointer
   的 ``pedagogy`` 频道里, 不新增任何存储;
3. **软护栏一轮一次**: 装配时的字数是上一轮的累计值, 两边都发就会出现两张只有数字
   不同的「你说得太少」卡片(这条只有走 HTTP 才暴露: 单测里 ``system_chars`` 恒为 0);
4. **护栏照实改写并告知**: ``monologue_cap`` 触发时, 流里已经出门的原文收不回来,
   所以 ``learn.guard.data.replacement`` 带全文、溢出转 ``learn.attachment`` 卡片,
   而会话预览存的仍是原文 —— 这个分叉是刻意取舍, 要被断言住而不是被忘掉;
5. **不该教学时零打扰**: 闲聊与 ``PEDAGOGY_ENABLED=false`` 时一个 ``learn`` 字段都没有
   (匿名的情形在 ``tests/test_chat_api.py``, 那里的 client 本来就不带登录态)。

``MockChatModel`` 逐 2 字 ``sleep(0.02)`` 以模拟流式打字, 一次完整回合要 1~2 秒; 因此
这里把「同一场对话」抽成 module 级 fixture 复用响应, 而不是每个用例各发一轮。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from app.config import get_settings
from app.db.session import Base, get_db
from app.deps import AuthContext, get_current_optional
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ACCOUNT_ID = 7
CHAT = "/api/chat"
#: 超过 ``min_explain_chars``(80)的自述; 与概念名无关的句子, 见 ``Loop`` 的说明。
EXPLANATION = (
    "函数把创建它时的那层作用域记住了，"
    "所以哪怕它后来被传到别处调用，也照样读得到当时那些变量，等于随身带着环境。"
) * 2
OPENING = "我想搞懂闭包"


@dataclass(slots=True)
class Env:
    client: TestClient
    factory: async_sessionmaker[AsyncSession]
    _counter: Iterator[str]

    def say(self, text: str, conversation_id: str) -> str:
        """发一句话, 返回原始 SSE 文本(顺带断言流一定以 ``[DONE]`` 收尾)。"""
        response = self.client.post(
            CHAT,
            json={
                "messages": [{"role": "user", "content": text}],
                "conversation_id": conversation_id,
            },
        )
        assert response.status_code == 200, response.text
        assert response.text.rstrip().endswith("data: [DONE]")
        return response.text

    def new_conversation(self) -> str:
        """一个没用过的会话号(载体按 ``thread_id`` 隔离, 复用会串轮)。"""
        return next(self._counter)

    def thread_row(self, conversation_id: str) -> Any:
        """读回 ``agent_threads`` 行: 验证教学回合没有绕过会话建档/预览更新。"""
        import asyncio

        from app.models import AgentThread

        async def _read() -> Any:
            async with self.factory() as session:
                statement = select(AgentThread).where(AgentThread.thread_id == conversation_id)
                return (await session.execute(statement)).scalar_one_or_none()

        return asyncio.run(_read())


@dataclass(slots=True)
class Loop:
    """一场跑完「命名概念 -> 交自述 -> 再说一句」的三轮对话的原始 SSE。"""

    conversation_id: str
    opening: str
    explained: str
    idle: str


def frames(text: str) -> list[dict[str, Any]]:
    """SSE 里所有 JSON 帧(按到达顺序), ``[DONE]`` 除外。"""
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def learn_frames(text: str) -> list[dict[str, Any]]:
    return [frame["learn"] for frame in frames(text) if frame.get("learn")]


def learns(text: str, event_type: str) -> list[dict[str, Any]]:
    return [item for item in learn_frames(text) if item["type"] == event_type]


def content_of(text: str) -> str:
    """学习者实际看到的文本(所有 ``delta.content`` 拼接)。"""
    return "".join(
        frame["choices"][0]["delta"].get("content") or ""
        for frame in frames(text)
        if frame.get("choices")
    )


def first_content_index(text: str) -> int:
    """第一个带正文的帧的下标(``learn`` 帧要排在它前面)。"""
    return next(
        index
        for index, frame in enumerate(frames(text))
        if frame.get("choices") and frame["choices"][0]["delta"].get("content")
    )


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    """真 app + 固定登录态 + 文件版 sqlite(会话元数据); Redis 死端口走限流降级。

    - ``get_current_optional`` 覆盖成固定登录态: 教学循环要 ``account_id`` 做证据归属,
      而 ``CHAT_REQUIRE_AUTH=false`` 下真实依赖恒返回 ``None``;
    - ``get_db`` 指向 ``tmp_path`` 下的 sqlite 文件。``NullPool`` 让每个会话自己开连接,
      于是引擎的创建/销毁都不依赖 fixture 期的事件循环, teardown 也不必 dispose;
    - checkpointer 仍走 conftest 冻结的 ``CHECKPOINT_DB_PATH``, 由 app 的 lifespan 在
      TestClient 的事件循环里打开/关闭 —— 跨轮载体就存在那里, 一个字节都没多存。
    """
    import app.models  # noqa: F401 - 把表结构注册到 Base.metadata

    db_path = tmp_path_factory.mktemp("pedagogy-api") / "chat.sqlite"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
    factory = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_db():
        async with factory() as session:
            yield session

    async def _override_auth():
        return AuthContext(
            account_id=ACCOUNT_ID,
            session_id="sess-pedagogy",
            access_jti="jti-pedagogy",
            device_id=None,
            permissions={"*"},
        )

    # 多智能体化之后, 教学中间件挂在 **edu-agent 子图**上: 注册表为空会回落到
    # 内置默认(其中 edu-agent 是 paused), 教学频道整条下线。这里把默认注册表逐行
    # 种进 sqlite 并强制 edu-agent 为 active, 才能真的走到「注册表驱动运行时」
    # 这条路径(其余模块的死 MySQL 走的正是回落路径, 两边都被覆盖)。
    import asyncio

    from app.agent import invalidate_runtime
    from app.agent_registry import DEFAULT_AGENT_SPECS
    from app.models.agent import Agent

    async def _seed_agents() -> None:
        async with factory() as session:
            for spec in DEFAULT_AGENT_SPECS:
                session.add(
                    Agent(
                        agent_key=spec.agent_key,
                        name=spec.name,
                        role=spec.role,
                        status="active" if spec.agent_key == "edu-agent" else spec.status,
                        description=spec.description,
                        system_prompt=spec.system_prompt,
                        tool_refs=list(spec.tool_refs),
                        skill_refs=list(spec.skill_refs),
                        mcp_refs=list(spec.mcp_refs),
                        rag_refs=list(spec.rag_refs),
                        sort_order=spec.sort_order,
                    )
                )
            await session.commit()

    asyncio.run(_seed_agents())
    # 前面的测试模块可能已用回落注册表建过运行时: 丢弃, 让本模块按 sqlite 重建。
    invalidate_runtime()

    from app.main import app

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_optional] = _override_auth
    counter = iter(f"learn-{index}" for index in range(10_000))
    with TestClient(app) as client:
        yield Env(client=client, factory=factory, _counter=counter)
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def opening(env: Env) -> str:
    """一次「全新会话 + 命名概念」的回合(多个用例共享, 省一轮流式耗时)。"""
    return env.say(OPENING, env.new_conversation())


@pytest.fixture(scope="module")
def loop(env: Env) -> Loop:
    """一场完整的三轮对话。

    第二轮刻意用**不含「闭包」二字**的自述: 概念名只能来自跨轮载体, 于是
    ``concept_id == "闭包"`` 这条断言才真的在验证「频道活着」, 而不是在验证正则。
    """
    conversation = env.new_conversation()
    return Loop(
        conversation_id=conversation,
        opening=env.say(OPENING, conversation),
        explained=env.say(EXPLANATION, conversation),
        idle=env.say("我讲完了", conversation),
    )


# --------------------------------------------------------------------------- 状态先行


def test_first_learning_turn_opens_with_a_state_card(opening: str):
    states = learns(opening, "state")
    assert states, f"没有状态卡: {opening}"
    first = states[0]
    assert first["state"] == "Teach"
    assert first["loop_index"] == 1
    data = first["data"]
    assert data["concept_id"] == "闭包"
    assert data["actions"] == ["ask_explain"]
    assert data["persona"] == "feynman-tutor"
    assert data["probes_left"] == get_settings().pedagogy_max_probes_per_turn
    assert data["reason"]


def test_state_card_arrives_before_the_first_token(opening: str):
    """顺序是产品语义: 学习者先看到「现在在哪一步」, 再看到教练说话。"""
    assert first_content_index(opening) > 0
    assert frames(opening)[0]["learn"]["type"] == "state"


def test_learn_frames_keep_the_openai_envelope(opening: str):
    """``learn`` 帧必须带一个空 ``delta``: 只读 ``choices[].delta.content`` 的老客户端
    (包括 ``@ant-design/x-sdk`` 的默认 provider)才能自动忽略它, 不需要版本协商。"""
    assert learn_frames(opening)
    for frame in frames(opening):
        assert frame["choices"]
        if frame.get("learn"):
            assert frame["choices"] == [{"delta": {}}]


def test_reply_still_streams_alongside_the_learn_channel(opening: str):
    """教学增强不得吃掉聊天本身: 正文照常流式输出, 也没有降级提示。"""
    assert content_of(opening)
    assert "服务暂时不可用" not in content_of(opening)


def test_thread_metadata_is_recorded_for_a_learning_turn(env: Env, opening: str):
    """登录态下的教学回合仍然建档并更新预览(标题取首条用户消息)。"""
    conversation = env.new_conversation()
    env.say(OPENING, conversation)
    row = env.thread_row(conversation)
    assert row is not None
    assert row.account_id == ACCOUNT_ID
    assert row.title == OPENING
    assert row.last_message


# --------------------------------------------------------------------------- 跨轮


def test_second_turn_resumes_the_loop_from_the_checkpointer_channel(loop: Loop):
    states = learns(loop.explained, "state")
    assert states, f"第二轮没有状态卡: {loop.explained}"
    first = states[0]
    assert first["state"] == "Detect"
    assert first["data"]["actions"] == ["probe"]
    # 概念名不在这一轮的输入里, 只能来自 pedagogy 频道
    assert first["data"]["concept_id"] == "闭包"
    assert str(len(EXPLANATION)) in first["data"]["reason"]
    # 发言占比跨轮累计: 系统上一轮说过的话算进了分母
    assert first["data"]["talk_ratio"] > 0


def test_grading_state_explains_why_it_cannot_advance(loop: Loop):
    """``Detect`` 的推进要教练侧判分事件; 学习者再说一句只收到一条说明, 不伪造状态卡。"""
    events = learn_frames(loop.idle)
    assert events[0]["type"] == "note"
    assert "判分器" in events[0]["data"]["text"]
    assert events[0]["state"] == "Detect"
    # 状态没动就没有状态卡; 剩下只可能是模型说完之后的软护栏
    assert [item["type"] for item in events[1:]] in ([], ["guard"])
    assert all(item["data"]["guard"] == "learner_talk_ratio" for item in events[1:])


def test_a_fresh_conversation_does_not_inherit_the_old_loop(env: Env):
    """载体按 ``thread_id`` 隔离: 换个会话号就是新一轮, 不接着上一个概念算字数。"""
    assert learn_frames(env.say(EXPLANATION, env.new_conversation())) == []


# --------------------------------------------------------------------------- 软护栏


def test_soft_guard_is_reported_once_per_turn(loop: Loop):
    """装配时的字数是上一轮的累计值, 中间件在模型说完后再报一次 —— 只能留后者。"""
    ratio = [
        item
        for item in learns(loop.explained, "guard")
        if item["data"]["guard"] == "learner_talk_ratio"
    ]
    assert len(ratio) == 1, f"软护栏重复上报: {ratio}"
    data = ratio[0]["data"]
    assert data["action"] == "log"
    assert "replacement" not in data  # 软护栏只记录, 不改写
    # 学习者这一轮说的字数被记进了分子(占比的分母含系统刚说完的这条回复)
    assert f"learner={len(EXPLANATION)}" in data["detail"]
    system_chars = int(data["detail"].split("system=")[1].split(")")[0])
    assert system_chars >= len(content_of(loop.explained))


def test_soft_guard_stays_quiet_when_the_learner_talks_enough(env: Env):
    """占比达标就不该有卡片 —— 护栏是信号, 不是每轮都要刷的存在感。"""
    conversation = env.new_conversation()
    env.say(OPENING, conversation)
    text = env.say(EXPLANATION * 8, conversation)
    guards = [item["data"]["guard"] for item in learns(text, "guard")]
    assert "learner_talk_ratio" not in guards


# --------------------------------------------------------------------------- 硬护栏与附件


def test_monologue_cap_publishes_replacement_and_attachment(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    """流式输出已经出门, 服务端收不回来 -> 照实改写 + 明确告知(取舍见 agent_pedagogy)。"""
    monkeypatch.setattr(get_settings(), "pedagogy_monologue_cap_chars", 40)
    text = env.say(OPENING, env.new_conversation())
    streamed = content_of(text)

    caps = [item for item in learns(text, "guard") if item["data"]["guard"] == "monologue_cap"]
    assert len(caps) == 1
    data = caps[0]["data"]
    assert data["action"] == "truncate"
    replacement = data["replacement"]
    assert len(replacement) < len(streamed)
    # 前端拿 replacement 整体替换气泡, 因此它必须以被保留的那一段开头
    assert streamed.startswith(replacement.split("\n\n")[0])

    cards = learns(text, "attachment")
    assert len(cards) == 1
    assert cards[0]["data"]["title"]
    assert cards[0]["data"]["chars"] == len(cards[0]["data"]["text"])
    assert cards[0]["data"]["text"]


def test_thread_preview_records_the_streamed_text_not_the_rewrite(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    """会话元数据记的是**流式输出过的原文**; 学习证据的权威副本在账本(#7), 不在这里。"""
    monkeypatch.setattr(get_settings(), "pedagogy_monologue_cap_chars", 40)
    conversation = env.new_conversation()
    text = env.say(OPENING, conversation)
    streamed = content_of(text)

    # 先确认这一轮确实触发了改写, 否则「预览存原文」这条断言什么也没验证
    assert any(item["data"]["guard"] == "monologue_cap" for item in learns(text, "guard"))
    row = env.thread_row(conversation)
    assert row is not None and row.last_message
    assert streamed.startswith(row.last_message[:20])


# --------------------------------------------------------------------------- 零打扰


def test_chitchat_has_no_learn_channel(env: Env):
    text = env.say("你好", env.new_conversation())
    assert learn_frames(text) == []
    assert content_of(text)


def test_disabled_flag_removes_the_learn_channel(env: Env, monkeypatch: pytest.MonkeyPatch):
    """``PEDAGOGY_ENABLED=false`` 是全局降级开关: 内核出问题时聊天必须照常可用。"""
    monkeypatch.setattr(get_settings(), "pedagogy_enabled", False)
    text = env.say(OPENING, env.new_conversation())
    assert learn_frames(text) == []
    assert content_of(text)
    assert "服务暂时不可用" not in content_of(text)
