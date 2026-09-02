"""客户端 IP 解析: 按可信跳数从右向左取, 非法条目回退对端地址。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.core.net import resolve_client_ip
from starlette.requests import Request

PEER = "203.0.113.9"


def _request(xff: str | None = None, peer: str = PEER) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 55555),
    }
    return Request(scope)


@pytest.fixture()
def set_hops(monkeypatch):
    def _set(n: int) -> None:
        monkeypatch.setattr(
            "app.core.net.get_settings",
            lambda: SimpleNamespace(trusted_proxy_hops=n),
        )

    return _set


def test_hops_zero_ignores_forwarded_headers(set_hops):
    set_hops(0)
    assert resolve_client_ip(_request("10.0.0.1, 10.0.0.2")) == PEER


def test_hops_one_takes_rightmost_entry(set_hops):
    set_hops(1)
    assert resolve_client_ip(_request("1.1.1.1, 10.0.0.1")) == "10.0.0.1"


def test_hops_two_takes_second_from_right(set_hops):
    set_hops(2)
    assert resolve_client_ip(_request("1.1.1.1, 10.0.0.1, 10.0.0.2")) == "10.0.0.1"


def test_client_cannot_spoof_beyond_trusted_hops(set_hops):
    set_hops(1)
    # 客户端伪造的最左条目不应被采信
    assert resolve_client_ip(_request("6.6.6.6, 10.0.0.1")) == "10.0.0.1"


def test_invalid_entry_falls_back_to_peer(set_hops):
    set_hops(1)
    assert resolve_client_ip(_request("10.0.0.1, junk")) == PEER


def test_fewer_entries_than_hops_takes_leftmost(set_hops):
    set_hops(3)
    assert resolve_client_ip(_request("1.1.1.1, 2.2.2.2")) == "1.1.1.1"


def test_missing_header_falls_back_to_peer(set_hops):
    set_hops(1)
    assert resolve_client_ip(_request(None)) == PEER
