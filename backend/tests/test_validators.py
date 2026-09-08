"""归一化校验单测: 头像 URL 的放行/拒绝边界(手机号、邮箱走既有实现)。"""
from __future__ import annotations

import pytest
from app.core.validators import normalize_avatar_url, normalize_email, normalize_phone
from app.exceptions import BizError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://cdn.example.com/a.png", "https://cdn.example.com/a.png"),
        ("http://cdn.example.com/a.png", "http://cdn.example.com/a.png"),
        ("  https://cdn.example.com/a.png  ", "https://cdn.example.com/a.png"),
        ("HTTPS://CDN.example.com/a.png", "HTTPS://CDN.example.com/a.png"),
        ("https://cdn.example.com/a.png?x=1#f", "https://cdn.example.com/a.png?x=1#f"),
        ("https://" + "a" * 500, "https://" + "a" * 500),  # 512 上限内
    ],
)
def test_normalize_avatar_url_accepts(raw: str, expected: str):
    assert normalize_avatar_url(raw) == expected


def test_normalize_avatar_url_empty_means_clear():
    assert normalize_avatar_url("") == ""
    assert normalize_avatar_url("   ") == ""


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://cdn.example.com/a.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "cdn.example.com/a.png",  # 缺 scheme
        "/relative/a.png",
        "https://",  # 无 netloc
        "https:// /a.png",  # 内部空白
        "https://cdn.example.com/a\n.png",  # 换行注入
        "https://cdn.example.com/" + "a" * 600,  # 超长
    ],
)
def test_normalize_avatar_url_rejects(raw: str):
    with pytest.raises(BizError) as exc_info:
        normalize_avatar_url(raw)
    assert int(exc_info.value.code) == 40000
    assert exc_info.value.http_status == 400


def test_normalize_phone_and_email_still_work():
    assert normalize_phone("+86 138-0000-0000") == "13800000000"
    assert normalize_email("  Foo@Example.com ") == "foo@example.com"
    with pytest.raises(BizError):
        normalize_phone("12345")
    with pytest.raises(BizError):
        normalize_email("not-an-email")
