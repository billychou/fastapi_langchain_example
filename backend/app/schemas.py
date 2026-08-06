"""Request / response schemas."""

from pydantic import BaseModel, Field


class MessageType(BaseModel):
    role: str = Field(..., description="消息角色：user / assistant / system")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    messages: list[MessageType] = Field(default=[], description="对话消息列表（OpenAI 风格）")
    conversation_id: str = Field(default="default", description="会话 ID，用于隔离多轮对话记忆")
