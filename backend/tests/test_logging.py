"""结构化日志: request-id 贯穿 + text/JSON 双格式。"""

from __future__ import annotations

import json
import logging
import sys

from app.core.log import HANDLER_NAME, JsonFormatter, RequestIdFilter, request_id_var, setup_logging


def _record(msg: str = "hello", exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, (), exc_info)


def test_request_id_filter_reads_contextvar():
    record = _record()
    token = request_id_var.set("rid-123")
    try:
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "rid-123"
    finally:
        request_id_var.reset(token)

    # 请求上下文之外取默认值
    record2 = _record()
    RequestIdFilter().filter(record2)
    assert record2.request_id == "-"


def test_json_formatter_shape():
    record = _record()
    record.request_id = "rid-1"
    out = json.loads(JsonFormatter().format(record))
    assert out["level"] == "INFO"
    assert out["logger"] == "t"
    assert out["message"] == "hello"
    assert out["request_id"] == "rid-1"
    assert out["ts"].startswith("2")  # ISO 时间, 粗略断言


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record(exc_info=sys.exc_info())
    record.request_id = "-"
    out = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in out["exc"]


def test_setup_logging_idempotent():
    setup_logging(fmt="text", app_env="production")
    setup_logging(fmt="text", app_env="production")
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, "name", None) == HANDLER_NAME]
    assert len(ours) == 1


def test_setup_logging_auto_format():
    setup_logging(fmt="auto", app_env="production")
    (handler,) = [h for h in logging.getLogger().handlers if h.name == HANDLER_NAME]
    assert isinstance(handler.formatter, JsonFormatter)
    setup_logging(fmt="auto", app_env="dev")
    (handler,) = [h for h in logging.getLogger().handlers if h.name == HANDLER_NAME]
    assert not isinstance(handler.formatter, JsonFormatter)


def test_request_context_propagates_to_logs():
    """HTTP 请求内产生的日志自动携带该请求的 request-id(含访问日志)。"""
    from app.main import app
    from fastapi.testclient import TestClient

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collector()
    handler.addFilter(RequestIdFilter())
    target = logging.getLogger("auth.http")
    target.addHandler(handler)
    try:
        with TestClient(app) as client:
            response = client.get("/api/health", headers={"X-Request-Id": "rid-e2e-1"})
        assert response.headers["X-Request-Id"] == "rid-e2e-1"
    finally:
        target.removeHandler(handler)

    access = [r for r in records if "GET /api/health" in r.getMessage()]
    assert access
    assert all(r.request_id == "rid-e2e-1" for r in access)
