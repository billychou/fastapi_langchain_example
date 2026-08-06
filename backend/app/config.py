"""Application settings loaded from environment / .env file.

合并自: 原聊天后端配置(LLM) + 用户认证系统配置(JWT/Redis/MySQL/风控/PII)。
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = (
    "你是一个乐于助人的智能助理。你可以查询当前时间、做数学计算、查询示例天气。"
    "请用中文回答，回答尽量简洁清晰。"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ================= 应用 =================
    app_name: str = "backend"
    app_env: str = "production"  # dev / staging / production
    debug: bool = False
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173"

    # ================= LLM (聊天 Agent) =================
    llm_provider: str = "openai"  # openai | anthropic | mock
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    chat_require_auth: bool = True  # /api/chat 是否要求登录(企业默认开启)

    # ================= MySQL =================
    database_url: str = (
        "mysql+aiomysql://auth_user:CHANGE_ME@127.0.0.1:3306/auth_service?charset=utf8mb4"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800  # 小于 MySQL wait_timeout, 防连接被服务端掐断

    # ================= Redis =================
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ================= JWT 双 Token =================
    jwt_secret_key: str = ""  # 生产必填: >=32 字节随机串; 多服务验签建议 RS256
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "auth-service"
    jwt_leeway_seconds: int = 30
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 3600
    refresh_token_rotation: bool = True
    strict_session_check: bool = True  # Access 校验时查 Redis 会话(踢人即时生效)

    # ================= 登录策略 =================
    login_policy_default: str = "multi_device"  # multi_device | single_device

    # ================= Argon2id (OWASP 推荐参数) =================
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 64 * 1024
    argon2_parallelism: int = 4

    # ================= 限流(令牌桶) =================
    rl_login_ip_capacity: int = 10
    rl_login_ip_refill_per_min: float = 5.0
    rl_login_account_capacity: int = 5
    rl_login_account_refill_per_min: float = 2.0
    rl_sms_per_minute: int = 1
    rl_sms_per_day: int = 10
    rl_sms_ip_capacity: int = 20
    rl_sms_ip_refill_per_min: float = 10.0

    # ================= 登录失败锁定 =================
    login_fail_window_seconds: int = 600
    login_max_failures: int = 5
    login_lock_base_seconds: int = 900
    login_lock_max_seconds: int = 24 * 3600
    login_captcha_threshold: int = 3
    register_require_sms: bool = False  # 注册是否强制短信验证码(生产建议 True)

    # ================= RBAC 权限缓存 =================
    rbac_cache_ttl_seconds: int = 300

    # ================= PII 字段加密 =================
    pii_active_key_id: str = "k1"
    pii_keys: str = ""  # 格式 "k1:<base64-32B>,k2:<base64-32B>" 支持轮转保留旧版本
    pii_blind_index_key: str = ""  # HMAC 盲索引独立密钥(base64-32B)

    # ------------------------------------------------------------------
    @field_validator("pii_keys")
    @classmethod
    def _validate_pii_keys(cls, v: str) -> str:
        if not v:
            return v
        for part in v.split(","):
            key_id, _, b64 = part.partition(":")
            if not key_id or not b64:
                raise ValueError(f"非法 PII 密钥配置: {part!r}, 期望 'key_id:base64(32B)'")
            try:
                raw = base64.b64decode(b64)
            except binascii.Error as exc:
                raise ValueError(f"PII 密钥 {key_id} 不是合法 base64") from exc
            if len(raw) != 32:
                raise ValueError(f"PII 密钥 {key_id} 必须为 32 字节(AES-256)")
        return v

    # ------------------------------------------------------------------
    @property
    def pii_keyring(self) -> dict[str, bytes]:
        ring: dict[str, bytes] = {}
        for part in filter(None, self.pii_keys.split(",")):
            key_id, _, b64 = part.partition(":")
            ring[key_id.strip()] = base64.b64decode(b64.strip())
        return ring

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
