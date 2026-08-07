"""Agent 会话元数据请求/响应模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ThreadItem(BaseModel):
    thread_id: str = Field(..., description="会话 ID(与 LangGraph thread_id 一致)")
    title: str = Field(..., description="会话标题")
    last_message: str | None = Field(default=None, description="最新助手回复预览")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="最后对话时间")


class CreateThreadRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255, description="会话标题, 缺省为「新会话」")


class UpdateThreadRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="新标题")


class ThreadMessage(BaseModel):
    role: str = Field(..., description="消息角色: user / assistant")
    content: str = Field(..., description="消息内容")
