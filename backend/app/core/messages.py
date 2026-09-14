"""LangChain 消息内容的归一化(纯函数, 无依赖)。

``BaseMessage.content`` 既可能是 ``str``, 也可能是 content blocks 列表
(``[{"type": "text", "text": "..."}]``, 多模态模型与部分 provider 会用),
所以任何要「数一数系统说了多少字」的地方都必须先归一化 —— 直接 ``len(message.content)``
在 blocks 形态下数出来的是列表长度(通常是 1), 于是 ``monologue_cap`` 与
``learner_talk_ratio`` 两条护栏会静默失效。

历史上 ``app/api/chat.py`` 与 ``app/services/thread_service.py`` 各有一份同样的实现;
第三份(教学中间件)出现时收拢到这里。
"""

from __future__ import annotations

from typing import Any


def message_text(content: Any) -> str:
    """把消息内容归一化成纯文本; 无法识别的形态返回空串(不抛异常)。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""
