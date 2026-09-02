"""客户端真实来源 IP 解析。

安全相关决策(限流/锁定/审计)统一使用 `resolve_client_ip()`, 禁止直接取
`request.client.host` —— 部署在 Nginx/LB 之后时它只是网关 IP, 会导致
所有用户共享同一个限流桶, 风控形同虚设。

配置 `TRUSTED_PROXY_HOPS`:
- 0(默认): 不信任任何代理头, 直接返回对端 IP(裸机/单容器部署);
- 1: 服务前有一层可信代理(如 Nginx), 取 X-Forwarded-For 最右条目;
- N: 有 N 层可信代理时, 从右向左取第 N 个条目。
XFF 链中越靠左越不可信(可被客户端伪造), 因此只按可信跳数从右向左取,
且条目必须是合法 IP, 否则回退对端 IP。
"""
from __future__ import annotations

import ipaddress

from fastapi import Request

from app.config import get_settings


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def resolve_client_ip(request: Request) -> str:
    """返回用于风控/审计的客户端 IP; 无法可信解析时回退为对端地址。"""
    peer = request.client.host if request.client else "-"
    hops = get_settings().trusted_proxy_hops
    if hops <= 0:
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not parts:
        return peer
    candidate = parts[-hops] if len(parts) >= hops else parts[0]
    return candidate if _is_valid_ip(candidate) else peer
