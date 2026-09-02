"""结构化日志: contextvars 贯穿 request-id + text/JSON 双格式。

设计要点(对齐 Dify 等生产级后端):

- ``RequestContextMiddleware`` 在请求入口把 request-id 写入
  ``request_id_var``; contextvars 随协程/子任务传播, 因此同一请求内
  后续任意模块(含 agent/LLM 调用链、SSE 流式生成器)的日志自动携带
  request-id, 无需逐层显式传参。
- ``LOG_FORMAT=auto``(默认): dev/staging 输出人类可读 text, production
  输出单行 JSON, 便于 Loki/ELK 等采集; 可用 text/json 强制覆盖。
- ``setup_logging`` 幂等: 重复调用只替换自己挂载的 handler, 不影响
  uvicorn 等第三方日志器。
"""

from __future__ import annotations

import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Any

# 请求作用域 request-id; 请求上下文之外取默认值 "-"
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

HANDLER_NAME = "app-structured"

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [rid=%(request_id)s] %(message)s"


class RequestIdFilter(logging.Filter):
    """为每条日志记录附加 ``request_id`` 字段(取自当前上下文)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志, 面向生产环境日志采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    *,
    level: int = logging.INFO,
    fmt: str = "auto",
    app_env: str = "production",
) -> None:
    """配置根日志器; 幂等, 可安全重复调用(测试/多次建应用)。"""
    resolved = fmt
    if resolved not in ("text", "json"):
        resolved = "json" if app_env == "production" else "text"

    handler = logging.StreamHandler()
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if resolved == "json" else logging.Formatter(TEXT_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        if getattr(existing, "name", None) == HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
