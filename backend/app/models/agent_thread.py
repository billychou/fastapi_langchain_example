"""Agent 会话元数据表: 与 LangGraph thread_id 一一对应。

只存元数据(标题/最后消息预览/时间戳); 完整对话记忆由 LangGraph checkpointer 持有。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _new_thread_id() -> str:
    return str(uuid.uuid4())


class AgentThread(Base):
    """会话元数据: 列表页展示标题/预览, 按 updated_at 倒序。"""

    __tablename__ = "agent_threads"

    thread_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=_new_thread_id, comment="与 LangGraph thread_id 一致(uuid4)"
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), comment="所属账号ID"
    )
    title: Mapped[str] = mapped_column(String(255), default="新会话", comment="首条用户消息截断或手动重命名")
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最新助手回复预览")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )
