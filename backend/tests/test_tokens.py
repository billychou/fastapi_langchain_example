"""双 Token 签发与校验: 类型/过期/篡改/黑名单 TTL。"""
from __future__ import annotations

import pytest
from app.core.tokens import (
    access_ttl_remaining,
    build_token,
    decode_token,
    encode_refresh_with_jti,
)
from app.exceptions import AuthError


def _issue(token_type: str = "access", ttl: int = 900) -> tuple[str, str]:
    return build_token(
        account_id=42, session_id="sid-1", token_type=token_type, ttl_seconds=ttl
    )


def test_access_roundtrip():
    token, jti = _issue()
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == "42"
    assert payload["sid"] == "sid-1"
    assert payload["jti"] == jti
    assert payload["typ"] == "access"


def test_refresh_type_mismatch_rejected():
    token, _ = _issue(token_type="refresh", ttl=3600)
    with pytest.raises(AuthError):
        decode_token(token, expected_type="access")


def test_expired_token_rejected():
    token, _ = _issue(ttl=-3600)
    with pytest.raises(AuthError, match="过期"):
        decode_token(token, expected_type="access")


def test_tampered_signature_rejected():
    token, _ = _issue()
    head, _, sig = token.rpartition(".")
    flipped = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    with pytest.raises(AuthError):
        decode_token(f"{head}.{flipped}", expected_type="access")


def test_garbage_token_rejected():
    with pytest.raises(AuthError):
        decode_token("not-a-jwt", expected_type="access")


def test_ttl_remaining_bounded_and_non_negative():
    token, _ = _issue(ttl=600)
    payload = decode_token(token, expected_type="access")
    assert 0 < access_ttl_remaining(payload) <= 600
    assert access_ttl_remaining({"exp": 0}) == 0


def test_encode_refresh_with_jti_keeps_jti():
    token = encode_refresh_with_jti(
        account_id=7, session_id="s2", jti="fixed-jti", ttl_seconds=60
    )
    payload = decode_token(token, expected_type="refresh")
    assert payload["jti"] == "fixed-jti"
    assert payload["sid"] == "s2"
