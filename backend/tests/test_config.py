"""配置安全闸门: 生产环境密钥校验与 PII 密钥格式校验。

直接实例化 Settings(_env_file=None) 并清空相关环境变量, 与运行时 .env 完全隔离。
"""
from __future__ import annotations

import base64
import hashlib

import pytest
from app.config import (
    DEMO_JWT_SECRET,
    DEMO_PII_BLIND_INDEX_KEY,
    DEMO_PII_KEYS,
    Settings,
)
from pydantic import ValidationError

_SECRET_VARS = ("JWT_SECRET_KEY", "PII_KEYS", "PII_BLIND_INDEX_KEY", "APP_ENV")


def _b64_32(seed: bytes) -> str:
    return base64.b64encode(hashlib.sha256(seed).digest()).decode()


def _strong() -> dict[str, str]:
    return {
        "jwt_secret_key": hashlib.sha256(b"prod-secret").hexdigest(),
        "pii_keys": f"k1:{_b64_32(b'prod-k1')}",
        "pii_blind_index_key": _b64_32(b"prod-blind"),
    }


@pytest.fixture()
def clean_env(monkeypatch):
    for var in _SECRET_VARS:
        monkeypatch.delenv(var, raising=False)


def _production(**overrides) -> Settings:
    return Settings(_env_file=None, app_env="production", **overrides)


def test_production_rejects_missing_secrets(clean_env):
    with pytest.raises(ValidationError, match="生产环境启动校验失败"):
        _production()


def test_production_rejects_demo_placeholders(clean_env):
    with pytest.raises(ValidationError, match="占位值|演示密钥"):
        _production(
            jwt_secret_key=DEMO_JWT_SECRET,
            pii_keys=DEMO_PII_KEYS,
            pii_blind_index_key=DEMO_PII_BLIND_INDEX_KEY,
        )


def test_production_rejects_short_jwt_secret(clean_env):
    with pytest.raises(ValidationError, match="长度不足"):
        _production(**(_strong() | {"jwt_secret_key": "too-short"}))


def test_production_accepts_strong_secrets(clean_env):
    settings = _production(**_strong())
    assert settings.app_env == "production"


def test_dev_env_warns_but_boots_on_weak_secrets(clean_env):
    settings = Settings(_env_file=None, app_env="dev")
    assert settings.app_env == "dev"


def test_pii_keys_malformed_entry_rejected(clean_env):
    with pytest.raises(ValidationError, match="非法 PII 密钥配置"):
        _production(**(_strong() | {"pii_keys": "missing-colon-and-base64"}))


def test_pii_keys_invalid_base64_rejected(clean_env):
    with pytest.raises(ValidationError, match="不是合法 base64"):
        _production(**(_strong() | {"pii_keys": "k1:!!!not-base64!!!"}))


def test_pii_keys_wrong_length_rejected(clean_env):
    short = base64.b64encode(b"x" * 16).decode()
    with pytest.raises(ValidationError, match="32 字节"):
        _production(**(_strong() | {"pii_keys": f"k1:{short}"}))


def test_pii_keyring_parses_multiple_versions(clean_env):
    overrides = _strong() | {"pii_keys": f"k1:{_b64_32(b'a')},k2:{_b64_32(b'b')}"}
    settings = _production(**overrides)
    ring = settings.pii_keyring
    assert set(ring) == {"k1", "k2"}
    assert all(len(k) == 32 for k in ring.values())


def test_cors_origin_list_splits_and_strips(clean_env):
    overrides = _strong() | {"cors_origins": "http://a.com, ,http://b.com"}
    settings = _production(**overrides)
    assert settings.cors_origin_list == ["http://a.com", "http://b.com"]
