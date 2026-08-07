"""Agent 会话元数据路由: 列表/新建/重命名/删除 + 历史消息读取。

历史消息直接读 LangGraph checkpointer 状态(当前为进程内 InMemorySaver,
服务重启后为空); 元数据(agent_threads)持久化在 MySQL, 重启不丢。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import get_agent
from app.db.session import get_db
from app.deps import AuthContext, get_current
from app.exceptions import BizCode, BizError
from app.models.agent_thread import AgentThread
from app.schemas import CreateThreadRequest, ThreadItem, ThreadMessage, UpdateThreadRequest
from app.schemas.common import ok
from app.services import thread_service

logger = logging.getLogger("threads")

router = APIRouter(prefix="/threads", tags=["threads"])


def _to_item(t: AgentThread) -> dict:
    return ThreadItem(
        thread_id=t.thread_id,
        title=t.title,
        last_message=t.last_message,
        created_at=t.created_at,
        updated_at=t.updated_at,
    ).model_dump(mode="json")


@router.get("", summary="当前用户的会话列表(按最后对话时间倒序)")
async def list_threads(
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
):
    threads = await thread_service.list_threads(db, ctx.account_id)
    return ok([_to_item(t) for t in threads])


@router.post("", summary="新建会话(标题缺省为「新会话」)")
async def create_thread(
    body: CreateThreadRequest | None = None,
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
):
    thread = await thread_service.create_thread(db, ctx.account_id, body.title if body else None)
    return ok(_to_item(thread))


@router.patch("/{thread_id}", summary="重命名会话")
async def rename_thread(
    body: UpdateThreadRequest,
    thread_id: str = Path(max_length=64),
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
):
    title = body.title.strip()
    if not title:
        raise BizError(BizCode.BAD_REQUEST, "标题不能为空")
    thread = await thread_service.rename_thread(db, ctx.account_id, thread_id, title)
    if thread is None:
        raise BizError(BizCode.NOT_FOUND, "会话不存在")
    return ok(_to_item(thread))


@router.delete("/{thread_id}", summary="删除会话(并尽力清理 LangGraph checkpoint)")
async def delete_thread(
    thread_id: str = Path(max_length=64),
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
):
    deleted = await thread_service.delete_thread(db, ctx.account_id, thread_id)
    if not deleted:
        raise BizError(BizCode.NOT_FOUND, "会话不存在")
    try:
        await get_agent().checkpointer.adelete_thread(thread_id)
    except Exception:  # noqa: BLE001 - checkpoint 清理失败不影响删除结果
        logger.warning("清理 LangGraph checkpoint 失败 thread_id=%s", thread_id, exc_info=True)
    return ok({"thread_id": thread_id})


@router.get("/{thread_id}/messages", summary="会话历史消息(读 LangGraph checkpoint, 服务重启后为空)")
async def get_thread_messages(
    thread_id: str = Path(max_length=64),
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
):
    thread = await thread_service.get_thread(db, ctx.account_id, thread_id)
    if thread is None:
        raise BizError(BizCode.NOT_FOUND, "会话不存在")

    state = await get_agent().aget_state({"configurable": {"thread_id": thread_id}})
    messages: list[dict] = []
    if state is not None:
        for m in state.values.get("messages", []):
            if isinstance(m, HumanMessage):
                messages.append(
                    ThreadMessage(role="user", content=thread_service.message_text(m.content)).model_dump()
                )
            elif isinstance(m, AIMessage):
                text = thread_service.message_text(m.content)
                if text:  # 跳过纯 tool-call 的空内容 AI 消息
                    messages.append(ThreadMessage(role="assistant", content=text).model_dump())
    return ok(messages)
