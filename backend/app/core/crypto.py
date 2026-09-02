"""PII 字段级加密(AES-256-GCM) + HMAC 盲索引 + 脱敏。

设计要点:
- 密文格式: nonce(12B) || ciphertext || tag(16B), 存 VARBINARY; 密钥版本单独存列, 支持轮转。
- 等值查询(按手机号/邮箱找账号)依赖 HMAC-SHA256 盲索引, 密钥与加密密钥隔离。
- 脱敏值在写入时生成, 展示场景永不解密。
"""
from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.exceptions import BizCode, BizError

_NONCE_LEN = 12


def _keyring() -> dict[str, bytes]:
    ring = get_settings().pii_keyring
    if not ring:
        raise BizError(BizCode.INTERNAL, "PII 加密密钥未配置(PII_KEYS)")
    return ring


def _blind_key() -> bytes:
    key_b64 = get_settings().pii_blind_index_key
    if not key_b64:
        raise BizError(BizCode.INTERNAL, "盲索引密钥未配置(PII_BLIND_INDEX_KEY)")
    import base64

    return base64.b64decode(key_b64)


def encrypt_field(plaintext: str, key_id: str | None = None) -> tuple[bytes, str]:
    """返回 (密文, key_id)。"""
    settings = get_settings()
    kid = key_id or settings.pii_active_key_id
    key = _keyring().get(kid)
    if key is None:
        raise BizError(BizCode.INTERNAL, f"PII 密钥版本不存在: {kid}")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ct, kid


def decrypt_field(blob: bytes, key_id: str) -> str:
    key = _keyring().get(key_id)
    if key is None:
        raise BizError(BizCode.INTERNAL, f"PII 密钥版本不存在: {key_id}")
    try:
        plain = AESGCM(key).decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], None)
    except Exception as exc:  # cryptography 抛出 InvalidTag 等
        raise BizError(BizCode.INTERNAL, "PII 解密失败(密钥不匹配或数据被篡改)") from exc
    return plain.decode("utf-8")


def blind_index(value: str, purpose: str) -> str:
    """HMAC-SHA256 盲索引: purpose 隔离手机/邮箱/证件命名空间。"""
    mac = hmac.new(_blind_key(), f"{purpose}:{value}".encode(), hashlib.sha256)
    return mac.hexdigest()


# ---------------- 脱敏 ----------------
def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else "***"


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain or not local:
        return "***"
    keep = local[0] if len(local) <= 2 else local[:2]
    return f"{keep}***@{domain}"


def mask_id_card(id_card: str) -> str:
    return f"{id_card[:3]}{'*' * (len(id_card) - 7)}{id_card[-4:]}" if len(id_card) >= 8 else "***"
