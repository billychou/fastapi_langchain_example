"""标识归一化与基础校验(手机号/邮箱/头像地址)。

生产建议: 手机号接入 phonenumbers 库做 E.164 归一化以支持国际号码。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.exceptions import BizCode, BizError

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")  # 中国大陆手机号
_URL_UNSAFE_RE = re.compile(r"[\s\x00-\x1f\x7f]")  # 空白与控制字符禁止出现在 URL 中
_AVATAR_MAX_LEN = 512  # 与 user_profile.avatar_url 列宽一致


def normalize_phone(raw: str) -> str:
    phone = re.sub(r"[\s\-\+]", "", raw.strip())
    if phone.startswith("86") and len(phone) == 13:
        phone = phone[2:]
    if not _PHONE_RE.fullmatch(phone):
        raise BizError(BizCode.BAD_REQUEST, "手机号格式不正确")
    return phone


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep or not local or "." not in domain or len(email) > 254:
        raise BizError(BizCode.BAD_REQUEST, "邮箱格式不正确")
    return email


def normalize_avatar_url(raw: str) -> str:
    """头像地址归一化: 空串原样返回(语义=清空头像), 其余必须是 http(s) 绝对 URL。

    只接受 http/https 是为了把 `javascript:` / `data:` 之类可执行 scheme 挡在库外,
    避免前端把用户可控字符串直接塞进 <img src> 或 <a href>。
    """
    url = raw.strip()
    if not url:
        return ""
    if (
        len(url) > _AVATAR_MAX_LEN
        or not url.lower().startswith(("http://", "https://"))
        or _URL_UNSAFE_RE.search(url)
        or not urlparse(url).netloc
    ):
        raise BizError(BizCode.BAD_REQUEST, "头像地址必须是 http(s) URL")
    return url
