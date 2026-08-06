"""全局配置: 通过环境变量 / .env 注入, 生产环境敏感项必须显式提供。"""
from __future__ import annotations

import base64
import binascii
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- 应用 ----
    app_name: str = "auth-service"
    app_env: str = "production"  # dev / staging / production
    debug: bool = False
    api_prefix: str = "/api/v1"
    cors_origins: str = ""  # 逗号分隔; 为空表示不开启 CORS

    # ---- MySQL ----
    database_url: str = (
        "mysql+aiomysql://auth_user:CHANGE_ME@127.0.0.1:3306/auth_service?charset=utf8mb4"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800  # 小于 MySQL wait_timeout, 防连接被服务端掐断

    # ---- Redis ----
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---- JWT 双 Token ----
    jwt_secret_key: str = ""  # 生产必填: >=32 字节随机串; 多服务验签场景建议换 RS256 非对称
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "auth-service"
    jwt_leeway_seconds: int = 30  # 时钟漂移容忍
    access_token_ttl_seconds: int = 15 * 60  # Access: 短过期 15min
    refresh_token_ttl_seconds: int = 30 * 24 * 3600  # Refresh: 长过期 30d
    refresh_token_rotation: bool = True  # 每次刷新轮换 Refresh Token(推荐开启)
    strict_session_check: bool = True  # Access 校验时强制查 Redis 会话(踢人即时生效)

    # ---- 登录策略 ----
    login_policy_default: str = "multi_device"  # multi_device | single_device

    # ---- Argon2id (OWASP 推荐参数) ----
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 64 * 1024  # 64 MiB
    argon2_parallelism: int = 4

    # ---- 限流(令牌桶): 登录接口 ----
    rl_login_ip_capacity: int = 10  # 桶容量
    rl_login_ip_refill_per_min: float = 5.0  # 每分钟补充令牌数
    rl_login_account_capacity: int = 5
    rl_login_account_refill_per_min: float = 2.0
    # ---- 限流: 验证码发送接口 ----
    rl_sms_per_minute: int = 1  # 同手机号每分钟最多 1 条
    rl_sms_per_day: int = 10  # 同手机号每天最多 10 条
    rl_sms_ip_capacity: int = 20
    rl_sms_ip_refill_per_min: float = 10.0

    # ---- 登录失败锁定 ----
    login_fail_window_seconds: int = 600  # 失败计数窗口
    login_max_failures: int = 5  # 窗口内失败 N 次触发锁定
    login_lock_base_seconds: int = 900  # 首次锁定时长
    login_lock_max_seconds: int = 24 * 3600  # 锁定时长上限(指数递增)
    login_captcha_threshold: int = 3  # 失败达到该次数后必须携带人机验证
    register_require_sms: bool = False  # 注册是否强制短信验证码(生产建议 True)

    # ---- RBAC 权限缓存 ----
    rbac_cache_ttl_seconds: int = 300

    # ---- PII 字段加密 ----
    pii_active_key_id: str = "k1"  # 当前写入使用的密钥版本
    pii_keys: str = ""  # 格式: "k1:<base64-32B>,k2:<base64-32B>" 支持轮转保留旧版本
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
        """解析 pii_keys 为 {key_id: 32字节密钥}。"""
        ring: dict[str, bytes] = {}
        for part in filter(None, self.pii_keys.split(",")):
            key_id, _, b64 = part.partition(":")
            ring[key_id.strip()] = base64.b64decode(b64.strip())
        return ring

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in filter(None, self.cors_origins.split(","))]


@lru_cache
def get_settings() -> Settings:
    return Settings()
