"""统一响应信封: {code, message, data}。"""
from __future__ import annotations

from typing import Any

from app.exceptions import BizCode


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": int(BizCode.OK), "message": message, "data": data}
