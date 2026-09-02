"""Application settings loaded from environment / .env file.

合并自: 原聊天后端配置(LLM) + 用户认证系统配置(JWT/Redis/MySQL/风控/PII)。
"""

from __future__ import annotations

import base64
import binascii
import logging
from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = (
    "你是一个乐于助人的智能助理。你可以查询当前时间、做数学计算、查询示例天气。"
    "请用中文回答，回答尽量简洁清晰。"
)

# ---------------------------------------------------------------------------
# .env.example 中的演示占位值; 生产环境(APP_ENV=production)禁止复用, 启动即失败。
# ---------------------------------------------------------------------------
DEMO_JWT_SECRET = "REPLACE_WITH_openssl_rand_base64_48"
DEMO_PII_KEYS = (
    "k1:WnM9vicjEZwds16p/bvI3Ywaci4PKMEBl/tZVUl9Flc=,"
    "k2:n5QGr8jgOA1i+kZOGk03LgpL8eeEJ2tB5SZrJNb1tAw="
)
DEMO_PII_BLIND_INDEX_KEY = "+Uj2zvu1ySBJ30/L5+YoPZxUWLztWyqhDKgu2aYh21s="


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
    checkpoint_backend: Literal["sqlite", "postgres"] = "sqlite"  # 会话记忆存储: sqlite(零依赖开发) / postgres(生产多副本)
    checkpoint_db_path: str = "./agent_checkpoints.sqlite"  # sqlite 后端时的本地文件路径
    checkpoint_database_url: str = ""  # postgres 后端必填, 如 postgres://user:pass@host:5432/langgraph

    # ================= 聊天接口保护 =================
    chat_max_messages: int = 100  # 单次请求消息条数上限
    chat_max_message_chars: int = 32_000  # 单条消息字符数上限
    rl_chat_capacity: int = 10  # 聊天令牌桶容量(登录按账号/匿名按IP)
    rl_chat_refill_per_min: float = 6.0  # 聊天令牌桶每分钟恢复速率
    trusted_proxy_hops: int = 0  # 可信代理跳数: >0 时按 X-Forwarded-For 从右向左取客户端 IP

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
    @model_validator(mode="after")
    def _assert_production_secrets(self) -> Settings:
        """密钥安全闸门:

        - 生产环境(APP_ENV=production): JWT/PII 密钥缺失、过短或仍是 .env.example
          占位值时直接拒绝启动(fail-fast), 防止空密钥签发 Token、演示密钥加密 PII。
        - 非生产环境: 仅告警, 便于本地无密钥演示。
        """
        problems: list[str] = []
        if not self.jwt_secret_key:
            problems.append("JWT_SECRET_KEY 未配置(生成: openssl rand -base64 48)")
        elif len(self.jwt_secret_key) < 32:
            problems.append("JWT_SECRET_KEY 长度不足 32 字符")
        elif self.jwt_secret_key == DEMO_JWT_SECRET:
            problems.append("JWT_SECRET_KEY 仍是 .env.example 占位值")
        if not self.pii_keys:
            problems.append("PII_KEYS 未配置(注册流程需加密手机号/邮箱)")
        elif self.pii_keys.replace(" ", "") == DEMO_PII_KEYS:
            problems.append("PII_KEYS 仍是 .env.example 演示密钥")
        if not self.pii_blind_index_key:
            problems.append("PII_BLIND_INDEX_KEY 未配置")
        elif self.pii_blind_index_key == DEMO_PII_BLIND_INDEX_KEY:
            problems.append("PII_BLIND_INDEX_KEY 仍是 .env.example 演示密钥")

        if self.app_env == "production":
            if problems:
                raise ValueError("生产环境启动校验失败: " + "; ".join(problems))
        elif problems:
            logging.getLogger("config").warning(
                "非生产环境检测到弱/占位密钥(%s); 生产部署前必须替换。", "; ".join(problems)
            )
        return self

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
