"""认证域请求/响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    identity_type: Literal["phone", "email"] = Field(description="注册方式")
    identifier: str = Field(description="手机号或邮箱")
    password: str = Field(min_length=8, max_length=128)
    nickname: str | None = Field(default=None, max_length=64)
    sms_code: str | None = Field(default=None, description="短信验证码(开启 register_require_sms 时必填)")


class LoginRequest(BaseModel):
    identity_type: Literal["phone", "email", "username"]
    identifier: str
    password: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(default=None, max_length=128, description="设备指纹(客户端生成)")
    captcha_token: str | None = Field(default=None, description="连续失败后必须携带")


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    all_devices: bool = Field(default=False, description="是否全端下线")


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


class SmsSendRequest(BaseModel):
    phone: str


class OAuthCallbackRequest(BaseModel):
    """演示用: 生产环境 openid 必须由服务端用 code 向 OAuth 提供方换取并校验。"""

    openid: str
    unionid: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    device_id: str | None = None


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    session_id: str


class AccountInfo(BaseModel):
    account_uuid: str
    nickname: str | None = None
    avatar_url: str | None = None
    phone_masked: str | None = None
    email_masked: str | None = None
    roles: list[str] = []
    login_policy: str = "multi_device"


class UpdateProfileRequest(BaseModel):
    """个人资料更新: 字段为 None 表示不修改; avatar_url 传空串表示清空头像。"""

    nickname: str | None = Field(default=None, max_length=64)
    avatar_url: str | None = Field(default=None, max_length=512)


class SessionInfo(BaseModel):
    session_id: str
    device_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    created_at: str | None = None
    last_seen: str | None = None
