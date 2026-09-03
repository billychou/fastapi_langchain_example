"""PII 字段级加密 / 盲索引 / 脱敏。"""
from __future__ import annotations

import pytest
from app.core.crypto import (
    blind_index,
    decrypt_field,
    encrypt_field,
    mask_email,
    mask_id_card,
    mask_phone,
)
from app.exceptions import BizError


def test_encrypt_decrypt_roundtrip():
    blob, key_id = encrypt_field("13800138000")
    assert key_id == "k1"
    assert b"13800138000" not in blob
    assert decrypt_field(blob, key_id) == "13800138000"


def test_encrypt_uses_fresh_nonce():
    blob_a, _ = encrypt_field("same-value")
    blob_b, _ = encrypt_field("same-value")
    assert blob_a != blob_b


def test_decrypt_with_wrong_key_version_fails():
    blob, _ = encrypt_field("secret", key_id="k1")
    with pytest.raises(BizError, match="解密失败"):
        decrypt_field(blob, "k2")


def test_decrypt_with_unknown_key_version_fails():
    blob, _ = encrypt_field("secret")
    with pytest.raises(BizError, match="密钥版本不存在"):
        decrypt_field(blob, "k9")


def test_blind_index_stable_and_namespaced():
    assert blind_index("a@b.com", "email") == blind_index("a@b.com", "email")
    assert blind_index("a@b.com", "email") != blind_index("a@b.com", "phone")


def test_masks():
    assert mask_phone("13800138000") == "138****8000"
    assert mask_phone("123") == "***"
    assert mask_email("alice@example.com") == "al***@example.com"
    assert mask_email("ab@example.com") == "a***@example.com"
    assert mask_id_card("110101199001011234") == "110***********1234"
    assert mask_id_card("short") == "***"
