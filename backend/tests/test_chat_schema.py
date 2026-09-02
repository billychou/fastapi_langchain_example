"""聊天请求输入保护: 消息条数/单条长度/会话 ID 上限。"""
from __future__ import annotations

import pytest
from app.config import get_settings
from app.schemas.chat import DEFAULT_CONVERSATION_ID, ChatRequest, MessageType
from pydantic import ValidationError


def test_rejects_too_many_messages():
    s = get_settings()
    messages = [MessageType(role="user", content="hi") for _ in range(s.chat_max_messages + 1)]
    with pytest.raises(ValidationError, match="消息过多"):
        ChatRequest(messages=messages)


def test_accepts_messages_at_limit():
    s = get_settings()
    messages = [MessageType(role="user", content="hi") for _ in range(s.chat_max_messages)]
    request = ChatRequest(messages=messages)
    assert len(request.messages) == s.chat_max_messages


def test_rejects_oversized_message():
    s = get_settings()
    with pytest.raises(ValidationError, match="单条消息过长"):
        ChatRequest(
            messages=[MessageType(role="user", content="x" * (s.chat_max_message_chars + 1))]
        )


def test_accepts_message_at_char_limit():
    s = get_settings()
    request = ChatRequest(
        messages=[MessageType(role="user", content="x" * s.chat_max_message_chars)]
    )
    assert len(request.messages[0].content) == s.chat_max_message_chars


def test_rejects_too_long_conversation_id():
    with pytest.raises(ValidationError):
        ChatRequest(conversation_id="c" * 65)


def test_default_conversation_id():
    request = ChatRequest(messages=[])
    assert request.conversation_id == DEFAULT_CONVERSATION_ID
