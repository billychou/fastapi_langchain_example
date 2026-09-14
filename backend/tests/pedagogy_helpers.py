"""教学内核测试的共用构造器(非测试模块, pytest 不收集)。

被 ``test_pedagogy_middleware.py`` / ``test_chat_pedagogy_api.py`` 复用: 一个按脚本回话
的假教练模型 + 事件载荷构造器 + context 装配。

刻意继承 ``BaseChatModel`` 而不是复用 ``app.agent.MockChatModel``: mock 模型按**关键词**
猜回复, 而中间件的测试需要「这一条消息带这些 ``pedagogy_events``」的精确控制 —— 猜出来的
脚本会让断言变成对正则的测试。
"""

from __future__ import annotations

from typing import Any

from app.context import ChatContext
from app.middleware.agent_pedagogy import EVENTS_KEY
from app.pedagogy.events import LoopCarrier, PedagogySink
from app.pedagogy.schemas import PedagogyState
from app.pedagogy.states import FeynmanState
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

ACCOUNT_ID = 7
THREAD_ID = "thread-pedagogy"


class ScriptedTutor(BaseChatModel):
    """按脚本回话的假教练: 每条回复 = ``(正文, additional_kwargs)``。

    - ``_generate`` 与 ``_astream`` 都实现: ``ainvoke`` 走前者, ``astream`` 走后者,
      只实现一个会让另一条路径落到 ``NotImplementedError``;
    - ``seen_prompts`` 记录模型真正收到的系统提示, 用于断言状态块被注入;
    - ``bind_tools`` 返回 self(agent 构造时会绑定工具集)。
    """

    script: tuple[tuple[str, dict[str, Any]], ...] = ()
    calls: int = 0
    seen_prompts: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-tutor"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedTutor:  # type: ignore[override]
        return self

    def _next(self, messages: list[BaseMessage]) -> tuple[str, dict[str, Any]]:
        for message in messages:
            if isinstance(message, SystemMessage):
                self.seen_prompts.append(str(message.content))
        index = min(self.calls, max(len(self.script) - 1, 0))
        self.calls += 1
        if not self.script:
            return "（脚本为空）", {}
        return self.script[index]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        content, extra = self._next(messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, additional_kwargs=extra))]
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        content, extra = self._next(messages)
        # 分片流出(而不是一次吐完): 中间件的护栏跑在流式之后, 测试要覆盖真实路径。
        for start in range(0, len(content), 4):
            piece = content[start : start + 4]
            chunk = AIMessageChunk(content=piece)
            if start + 4 >= len(content):
                chunk = AIMessageChunk(content=piece, additional_kwargs=extra)
            if run_manager is not None:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield ChatGenerationChunk(message=chunk)


def with_events(*events: dict[str, Any]) -> dict[str, Any]:
    """把若干内核事件包成 ``additional_kwargs``。"""
    return {EVENTS_KEY: list(events)}


def gap_event(
    *,
    gap_type: str = "causal_gap",
    probe: str = "那一步为什么可以这么做?",
    severity: int = 2,
    verdict: str = "not_closed",
    completeness: float = 0.5,
    own_words: float = 0.7,
    boundary: float = 0.2,
    system_chars: int = 999,
) -> dict[str, Any]:
    """一个 ``gap_reported`` 载荷(``system_chars`` 刻意给个大数: 中间件必须强制归零)。"""
    return {
        "kind": "gap_reported",
        "system_chars": system_chars,
        "report": {
            "gaps": [
                {
                    "type": gap_type,
                    "claim_id": "c1",
                    "probe": probe,
                    "severity": severity,
                    "supply": {
                        "atoms": [{"id": "atom:a", "statement": "闭包捕获的是变量本身"}],
                        "max_chars": 300,
                    },
                }
            ],
            "scores": {
                "completeness": completeness,
                "own_words": own_words,
                "boundary_awareness": boundary,
            },
            "verdict": verdict,
        },
    }


def supply_event(*, system_chars: int = 4242) -> dict[str, Any]:
    return {
        "kind": "supply_completed",
        "system_chars": system_chars,
        "bundle": {"atoms": [{"id": "atom:a", "statement": "闭包捕获的是变量本身"}], "max_chars": 300},
    }


def simplify_event(
    *,
    transfer_passed: bool = True,
    verdict: str = "closed",
    completeness: float = 0.9,
    own_words: float = 0.9,
    boundary: float = 0.9,
) -> dict[str, Any]:
    return {
        "kind": "simplify_judged",
        "transfer_passed": transfer_passed,
        "report": {
            "gaps": [],
            "scores": {
                "completeness": completeness,
                "own_words": own_words,
                "boundary_awareness": boundary,
            },
            "verdict": verdict,
        },
    }


def make_state(
    state: FeynmanState = FeynmanState.TEACH,
    *,
    concept_id: str = "闭包",
    loop_index: int = 1,
    learner_chars: int = 0,
    system_chars: int = 0,
) -> PedagogyState:
    return PedagogyState(
        thread_id=THREAD_ID,
        account_id=ACCOUNT_ID,
        learner_id=ACCOUNT_ID,
        concept_id=concept_id,
        state=state,
        loop_index=loop_index,
        learner_chars=learner_chars,
        system_chars=system_chars,
    )


def make_context(
    state: PedagogyState | None,
    *,
    sink: PedagogySink | None = None,
    step: Any = None,
    carrier: LoopCarrier | None = None,
    account_id: int = ACCOUNT_ID,
) -> tuple[ChatContext, PedagogySink | None]:
    """装配一个带教学上下文的 ``ChatContext``; ``sink`` 里预装 ``carrier``。"""
    if state is None:
        return ChatContext(account_id=0, authenticated=False), None
    collector = sink or PedagogySink()
    if carrier is None:
        carrier = LoopCarrier(state=state, turns=1)
    collector.carrier = carrier
    context = ChatContext(
        account_id=account_id,
        authenticated=True,
        permissions=frozenset({"*"}),
        learner_id=account_id,
        pedagogy_state=state,
        pedagogy_step=step,
        pedagogy_sink=collector,
    )
    return context, collector
