"""Request / response schemas."""

from pydantic import BaseModel, Field, model_validator

from app.config import get_settings

DEFAULT_CONVERSATION_ID = "default"


class MessageType(BaseModel):
    role: str = Field(..., description="消息角色：user / assistant / system")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    messages: list[MessageType] = Field(default=[], description="对话消息列表（OpenAI 风格）")
    conversation_id: str = Field(
        default=DEFAULT_CONVERSATION_ID,
        max_length=64,  # 与 DDL thread_id VARCHAR(64) / thread_service.THREAD_ID_MAX 对齐
        description="会话 ID，用于隔离多轮对话记忆",
    )

    @model_validator(mode="after")
    def _enforce_limits(self) -> "ChatRequest":
        """输入保护: 消息条数与单条长度上限(可配置), 防止超大请求打爆 LLM 链路。"""
        s = get_settings()
        if len(self.messages) > s.chat_max_messages:
            raise ValueError(f"单次请求消息过多(上限 {s.chat_max_messages} 条)")
        if any(len(m.content) > s.chat_max_message_chars for m in self.messages):
            raise ValueError(f"单条消息过长(上限 {s.chat_max_message_chars} 字符)")
        return self
