"""标识归一化与基础校验(手机号/邮箱)。

生产建议: 手机号接入 phonenumbers 库做 E.164 归一化以支持国际号码。
"""
from __future__ import annotations

import re

from app.exceptions import BizCode, BizError

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")  # 中国大陆手机号


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
