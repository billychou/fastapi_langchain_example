"""请求/响应模型包: chat(聊天) + auth(认证) + skills(技能) + common(统一信封)。"""
from app.schemas.auth import (
    AccountInfo,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    OAuthCallbackRequest,
    RefreshRequest,
    RegisterRequest,
    SessionInfo,
    SmsSendRequest,
    TokenPairResponse,
    UpdateLoginPolicyRequest,
    UpdateProfileRequest,
)
from app.schemas.chat import ChatRequest, MessageType
from app.schemas.common import ok
from app.schemas.skills import SkillDetail, SkillFile, SkillReloadResult, SkillSummary
from app.schemas.threads import (
    CreateThreadRequest,
    ThreadItem,
    ThreadMessage,
    UpdateThreadRequest,
)

__all__ = [
    "ChatRequest",
    "MessageType",
    "ok",
    "RegisterRequest",
    "LoginRequest",
    "RefreshRequest",
    "LogoutRequest",
    "ChangePasswordRequest",
    "SmsSendRequest",
    "OAuthCallbackRequest",
    "TokenPairResponse",
    "AccountInfo",
    "UpdateProfileRequest",
    "UpdateLoginPolicyRequest",
    "SessionInfo",
    "ThreadItem",
    "CreateThreadRequest",
    "UpdateThreadRequest",
    "ThreadMessage",
    "SkillSummary",
    "SkillDetail",
    "SkillFile",
    "SkillReloadResult",
]
