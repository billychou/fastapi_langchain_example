"""请求/响应模型包: chat(聊天) + auth(认证) + skills(技能) + common(统一信封)。

``learning`` 的模型(概念图 / 讲解证据 / 掌握度账本)**不在这里重导出**: 它们与
``app.pedagogy`` 的内核类型(``Claim`` / ``GapReport`` / ``EvidenceKind``)双向耦合,
集中重导出会让「内核模型」与「HTTP 模型」看起来是同一层东西。用的时候直接
``from app.schemas.learning import ...``。
"""
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
