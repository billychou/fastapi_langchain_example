"""Agent 会话元数据服务: CRUD + 聊天联动(ensure / record_exchange)。

- CRUD 面向 /api/v1/threads 路由, 均带属主(account_id)校验;
- ensure / record_exchange 面向 /api/chat 主链路, record_exchange 内的
  DB 失败仅告警不抛出, 保证 SSE 聊天不被元数据写入拖垮。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import BizCode, BizError
from app.models.agent_thread import AgentThread

logger = logging.getLogger("threads")

THREAD_ID_MAX = 64  # 与 DDL thread_id VARCHAR(64) 对齐
DEFAULT_TITLE = "新会话"
TITLE_MAX = 50      # 标题取首条用户消息前 50 字
PREVIEW_MAX = 200   # last_message 预览截断长度
LIST_LIMIT = 100    # v1 不分页


def truncate(text: str | None, limit: int) -> str:
    return (text or "").strip()[:limit]


def message_text(content: Any) -> str:
    """归一化 LangChain 消息内容(str 或 content blocks 列表)为纯文本。"""
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


async def list_threads(db: AsyncSession, account_id: int) -> list[AgentThread]:
    stmt = (
        select(AgentThread)
        .where(AgentThread.account_id == account_id)
        .order_by(AgentThread.updated_at.desc())
        .limit(LIST_LIMIT)
    )
    return list((await db.scalars(stmt)).all())


async def get_thread(db: AsyncSession, account_id: int, thread_id: str) -> AgentThread | None:
    """属主校验: 不存在或属于他人一律返回 None(对外统一 404)。"""
    thread = await db.get(AgentThread, thread_id)
    if thread is None or thread.account_id != account_id:
        return None
    return thread


async def create_thread(db: AsyncSession, account_id: int, title: str | None = None) -> AgentThread:
    thread = AgentThread(account_id=account_id, title=truncate(title, 255) or DEFAULT_TITLE)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


async def ensure_thread(db: AsyncSession, account_id: int, thread_id: str) -> AgentThread:
    """/api/chat 前置: 会话行不存在则创建; 属于他人则 404。"""
    thread = await db.get(AgentThread, thread_id)
    if thread is not None:
        if thread.account_id != account_id:
            raise BizError(BizCode.NOT_FOUND, "会话不存在")
        return thread
    thread = AgentThread(thread_id=thread_id, account_id=account_id, title=DEFAULT_TITLE)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


async def rename_thread(
    db: AsyncSession, account_id: int, thread_id: str, title: str
) -> AgentThread | None:
    thread = await get_thread(db, account_id, thread_id)
    if thread is None:
        return None
    thread.title = truncate(title, 255)
    await db.commit()
    await db.refresh(thread)
    return thread


async def delete_thread(db: AsyncSession, account_id: int, thread_id: str) -> bool:
    thread = await get_thread(db, account_id, thread_id)
    if thread is None:
        return False
    await db.delete(thread)
    await db.commit()
    return True


async def record_exchange(
    db: AsyncSession,
    account_id: int,
    thread_id: str,
    first_user_text: str,
    assistant_text: str,
) -> None:
    """一轮聊天成功结束后更新元数据:

    - last_message = 助手回复截断预览;
    - updated_at   = now();
    - 首轮(last_message 尚为 NULL)同时以首条用户消息截断生成 title。

    仅服务聊天主链路: 任何 DB 失败仅告警, 不向上抛。
    """
    try:
        thread = await db.get(AgentThread, thread_id)
        if thread is None or thread.account_id != account_id:
            return
        first_round = thread.last_message is None
        thread.last_message = truncate(assistant_text, PREVIEW_MAX) or None
        # 仅当标题仍为默认值时自动生成(不覆盖用户手动重命名)
        if first_round and thread.title == DEFAULT_TITLE and first_user_text.strip():
            thread.title = truncate(first_user_text, TITLE_MAX) or DEFAULT_TITLE
        thread.updated_at = func.now()  # type: ignore[assignment]
        await db.commit()
    except Exception:  # noqa: BLE001 - 元数据失败不影响聊天
        logger.warning("更新会话元数据失败 thread_id=%s", thread_id, exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
