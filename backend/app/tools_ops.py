"""运维连接器工具: Prometheus / Loki / Kubernetes。

三个工具都是**配置驱动 + mock 回退**:

- env 配了端点(``PROMETHEUS_URL`` / ``LOKI_URL`` / ``K8S_API_URL``)→ 真实 HTTP 调用,
  并把响应压成对 LLM 友好的紧凑 JSON(截断长时序/长日志, 保护上下文预算);
- 未配置或请求失败 → 返回结构化演示数据, 且带 ``source: "mock"`` 字段, 模型与用户
  都能一眼看出这是模拟结果。这与 ``MockChatModel`` 是同一哲学: **无凭证也能跑通整条
  智能体链路**(前端卡片、调度、工具链 UI 都不需要先把监控栈搭起来)。

生产环境建议 ``OPS_MOCK_FALLBACK=false``: 端点缺失或调用失败时返回
``source: "unavailable"``, 宁可让模型说「拿不到数据」, 也不要拿假指标误导运维决策。

返回统一是 JSON 字符串(``ensure_ascii=False``), 顶层必有 ``source``::

    {"source": "prometheus|loki|kubernetes|mock|error|unavailable", ...}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langchain.tools import tool

from app.config import Settings, get_settings

logger = logging.getLogger("tools.ops")

# ---------------------------------------------------------------------------
# 上下文预算护栏: 监控数据的原始体积可以很大, 交给模型前必须截断。
# ---------------------------------------------------------------------------
_MAX_SERIES = 8  # Prometheus 时序条数上限
_MAX_POINTS_PER_SERIES = 20  # 每条时序保留的采样点数(取最近的)
_MAX_LOG_LINES = 100  # Loki 单次返回日志行上限
_MAX_ITEMS = 50  # K8s 资源条目上限
_MAX_WINDOW_MINUTES = 24 * 60  # 回看窗口上限(1 天)

_MOCK_NOTE = "当前为演示数据(未接入真实端点或调用失败), 结论仅供联调参考, 不可用于生产决策。"


def _settings() -> Settings:
    """运行期读配置(单点函数, 便于测试注入不同端点)。"""
    return get_settings()


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _num(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else round(number, 6)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token.strip() else {}


def _parse_ts(value: Any) -> datetime | None:
    """解析 K8s 的 ``2026-10-16T02:00:00Z`` 风格时间戳。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(created_at: Any) -> str | None:
    stamp = _parse_ts(created_at)
    if stamp is None:
        return None
    seconds = int((datetime.now(UTC) - stamp).total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def _get_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """一次带超时的 GET, 返回 JSON 对象(非对象即抛 ValueError)。"""
    timeout = httpx.Timeout(_settings().ops_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, verify=verify, follow_redirects=True) as client:
        response = await client.get(url, params=dict(params or {}), headers=dict(headers or {}))
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("响应不是 JSON 对象")
    return payload


def _mock_or_unavailable(connector: str, reason: str, sample: dict[str, Any]) -> dict[str, Any]:
    """统一回退形状: 允许 mock 时返回演示数据, 否则明确告知不可用。"""
    if not _settings().ops_mock_fallback:
        return {"source": "unavailable", "connector": connector, "reason": reason}
    return {"source": "mock", "connector": connector, "reason": reason, "note": _MOCK_NOTE, **sample}


def _query_error(connector: str, payload: Mapping[str, Any], echo: dict[str, Any]) -> dict[str, Any]:
    """端点可达但查询被拒(如 PromQL/LogQL 语法错误): 原样回传, 让模型自己修正。"""
    error = payload.get("error") or payload.get("message") or payload.get("errorType") or "未知错误"
    return {"source": "error", "connector": connector, "error": str(error), **echo}


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------


def _mock_prometheus(promql: str, minutes: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    points = [
        [int((now - timedelta(minutes=minutes * ratio)).timestamp()), value]
        for ratio, value in ((1.0, 0.31), (0.6, 0.42), (0.3, 0.58), (0.1, 0.73), (0.0, 0.66))
    ]
    return {
        "query": promql,
        "window_minutes": minutes,
        "result_type": "matrix",
        "series_count": 2,
        "series": [
            {
                "metric": {"namespace": "prod", "pod": "checkout-6d4f7c8b9d-x2k4p"},
                "points": points,
                "latest": points[-1][1],
            },
            {
                "metric": {"namespace": "prod", "pod": "checkout-6d4f7c8b9d-m8zqw"},
                "points": [[ts, round(value * 0.8, 4)] for ts, value in points],
                "latest": round(points[-1][1] * 0.8, 4),
            },
        ],
    }


def _normalize_prometheus(payload: Mapping[str, Any], promql: str, minutes: int) -> dict[str, Any]:
    data = payload.get("data") or {}
    results = data.get("result") or []
    series: list[dict[str, Any]] = []
    for item in results[:_MAX_SERIES]:
        raw = item.get("values") or []
        points = [[_num(ts), _num(value)] for ts, value in raw[-_MAX_POINTS_PER_SERIES:]]
        if not points and isinstance(item.get("value"), (list, tuple)) and len(item["value"]) == 2:
            points = [[_num(item["value"][0]), _num(item["value"][1])]]
        series.append(
            {
                "metric": dict(item.get("metric") or {}),
                "points": points,
                "latest": points[-1][1] if points else None,
            }
        )
    out: dict[str, Any] = {
        "source": "prometheus",
        "query": promql,
        "window_minutes": minutes,
        "result_type": data.get("resultType"),
        "series_count": len(results),
        "series": series,
    }
    if len(results) > _MAX_SERIES:
        out["truncated_series"] = len(results) - _MAX_SERIES
        out["note"] = f"仅返回前 {_MAX_SERIES} 条时序, 请用更精确的 PromQL 收窄范围。"
    return out


@tool
async def prometheus_query(promql: str, minutes: int = 30) -> str:
    """查询 Prometheus 指标(PromQL), 返回最近一段时间的时序采样。

    promql 例: "sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)";
    minutes 是回看窗口(分钟, 默认 30, 上限 1440)。
    返回 JSON: series[].metric 是标签, series[].points 是 [时间戳, 值] 采样, latest 是最新值。
    """
    window = _clamp(int(minutes or 30), 1, _MAX_WINDOW_MINUTES)
    settings = _settings()
    base = settings.prometheus_url.strip().rstrip("/")
    echo = {"query": promql, "window_minutes": window}
    if not base:
        return _dumps(_mock_or_unavailable("prometheus", "未配置 PROMETHEUS_URL", _mock_prometheus(promql, window)))

    end = datetime.now(UTC)
    start = end - timedelta(minutes=window)
    params = {
        "query": promql,
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        # 目标 ~120 个点以内, 但不低于 15s 分辨率、不高于 10 分钟步长
        "step": max(15, min(600, window * 60 // 120)),
    }
    try:
        payload = await _get_json(
            f"{base}/api/v1/query_range", params=params, headers=_bearer(settings.prometheus_token)
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Prometheus 调用失败, 回退演示数据: %s", exc)
        return _dumps(_mock_or_unavailable("prometheus", f"调用失败: {exc}", _mock_prometheus(promql, window)))

    if payload.get("status") != "success":
        return _dumps(_query_error("prometheus", payload, echo))
    return _dumps(_normalize_prometheus(payload, promql, window))


# ---------------------------------------------------------------------------
# Loki
# ---------------------------------------------------------------------------


def _mock_loki(logql: str, limit: int, minutes: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    entries = [
        ("info", "checkout-6d4f7c8b9d-x2k4p", "订单创建成功 order_id=90271 latency_ms=83"),
        ("warn", "checkout-6d4f7c8b9d-x2k4p", "上游响应缓慢 payment-service latency_ms=1842"),
        ("error", "checkout-6d4f7c8b9d-m8zqw", "NullPointerException at com.shop.cart.CartService.sum(CartService.java:88)"),
        ("error", "gateway-7f9c5b6d4-t2p1n", "upstream connect error: connection timeout cluster=checkout"),
        ("info", "gateway-7f9c5b6d4-t2p1n", "GET /api/v1/checkout 504 5002ms"),
    ]
    streams: dict[str, dict[str, Any]] = {}
    for index, (level, pod, line) in enumerate(entries[:limit]):
        stream = streams.setdefault(
            pod, {"labels": {"namespace": "prod", "pod": pod}, "lines": []}
        )
        stamp = (now - timedelta(minutes=minutes, seconds=-index * 37)).isoformat()
        stream["lines"].append([stamp, f"{level.upper():5} {line}"])
    return {
        "query": logql,
        "window_minutes": minutes,
        "stream_count": len(streams),
        "line_count": sum(len(s["lines"]) for s in streams.values()),
        "streams": list(streams.values()),
    }


def _normalize_loki(payload: Mapping[str, Any], logql: str, limit: int, minutes: int) -> dict[str, Any]:
    data = payload.get("data") or {}
    results = data.get("result") or []
    streams: list[dict[str, Any]] = []
    total = 0
    budget = limit
    for item in results:
        if budget <= 0:
            break
        values = item.get("values") or []
        kept: list[list[Any]] = []
        for entry in values[:budget]:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            ts, line = entry[0], entry[1]
            stamp = datetime.fromtimestamp(int(ts) / 1e9, tz=UTC).isoformat() if str(ts).isdigit() else str(ts)
            kept.append([stamp, str(line)])
        if not kept:
            continue
        budget -= len(kept)
        total += len(kept)
        streams.append({"labels": dict(item.get("stream") or {}), "lines": kept})
    out: dict[str, Any] = {
        "source": "loki",
        "query": logql,
        "window_minutes": minutes,
        "stream_count": len(streams),
        "line_count": total,
        "streams": streams,
    }
    if len(results) > len(streams) or budget <= 0:
        out["truncated"] = True
    return out


@tool
async def loki_query(logql: str, limit: int = 50, minutes: int = 30) -> str:
    """查询 Loki 日志(LogQL), 用于筛选异常与定位报错。

    logql 例: '{namespace="prod"} |= "error"'; limit 是返回日志行上限(默认 50, 最大 100),
    minutes 是回看窗口(分钟, 默认 30, 上限 1440)。
    返回 JSON: streams[].labels 是日志流标签, streams[].lines 是 [时间, 日志原文]。
    """
    cap = _clamp(int(limit or 50), 1, _MAX_LOG_LINES)
    window = _clamp(int(minutes or 30), 1, _MAX_WINDOW_MINUTES)
    settings = _settings()
    base = settings.loki_url.strip().rstrip("/")
    echo = {"query": logql, "limit": cap, "window_minutes": window}
    if not base:
        return _dumps(_mock_or_unavailable("loki", "未配置 LOKI_URL", _mock_loki(logql, cap, window)))

    end = datetime.now(UTC)
    start = end - timedelta(minutes=window)
    params = {
        "query": logql,
        "limit": cap,
        "start": int(start.timestamp() * 1e9),  # Loki 用纳秒时间戳
        "end": int(end.timestamp() * 1e9),
        "direction": "backward",
    }
    try:
        payload = await _get_json(
            f"{base}/loki/api/v1/query_range", params=params, headers=_bearer(settings.loki_token)
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Loki 调用失败, 回退演示数据: %s", exc)
        return _dumps(_mock_or_unavailable("loki", f"调用失败: {exc}", _mock_loki(logql, cap, window)))

    if payload.get("status") not in (None, "success"):
        return _dumps(_query_error("loki", payload, echo))
    return _dumps(_normalize_loki(payload, logql, cap, window))


# ---------------------------------------------------------------------------
# Kubernetes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _K8sKind:
    api: str
    plural: str
    namespaced: bool


#: 支持的资源类型(规范名 → API 组/复数名/是否 namespaced)。
_K8S_KINDS: dict[str, _K8sKind] = {
    "pod": _K8sKind("/api/v1", "pods", True),
    "service": _K8sKind("/api/v1", "services", True),
    "configmap": _K8sKind("/api/v1", "configmaps", True),
    "secret": _K8sKind("/api/v1", "secrets", True),
    "serviceaccount": _K8sKind("/api/v1", "serviceaccounts", True),
    "event": _K8sKind("/api/v1", "events", True),
    "persistentvolumeclaim": _K8sKind("/api/v1", "persistentvolumeclaims", True),
    "node": _K8sKind("/api/v1", "nodes", False),
    "namespace": _K8sKind("/api/v1", "namespaces", False),
    "persistentvolume": _K8sKind("/api/v1", "persistentvolumes", False),
    "deployment": _K8sKind("/apis/apps/v1", "deployments", True),
    "statefulset": _K8sKind("/apis/apps/v1", "statefulsets", True),
    "daemonset": _K8sKind("/apis/apps/v1", "daemonsets", True),
    "replicaset": _K8sKind("/apis/apps/v1", "replicasets", True),
    "job": _K8sKind("/apis/batch/v1", "jobs", True),
    "cronjob": _K8sKind("/apis/batch/v1", "cronjobs", True),
    "ingress": _K8sKind("/apis/networking.k8s.io/v1", "ingresses", True),
    "horizontalpodautoscaler": _K8sKind("/apis/autoscaling/v2", "horizontalpodautoscalers", True),
}

#: 复数/别名 → 规范名("pods"、"Pod"、"hpa" 都能认)。
_KIND_LOOKUP: dict[str, str] = {name: name for name in _K8S_KINDS}
_KIND_LOOKUP.update({spec.plural: name for name, spec in _K8S_KINDS.items()})
_KIND_LOOKUP.update({"hpa": "horizontalpodautoscaler", "pvc": "persistentvolumeclaim", "svc": "service", "cm": "configmap", "ns": "namespace", "po": "pod", "deploy": "deployment", "sts": "statefulset", "ds": "daemonset"})


def _normalize_kind(kind: str) -> str | None:
    key = (kind or "").strip().lower().replace("_", "-")
    if not key:
        return None
    if key in _KIND_LOOKUP:
        return _KIND_LOOKUP[key]
    if key.endswith("s") and key[:-1] in _KIND_LOOKUP:
        return _KIND_LOOKUP[key[:-1]]
    return None


def _summarize(kind: str, obj: Mapping[str, Any]) -> dict[str, Any]:
    """把 K8s 资源对象压成诊断所需的最小字段集。"""
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    item: dict[str, Any] = {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created_at": meta.get("creationTimestamp"),
        "age": _age(meta.get("creationTimestamp")),
    }
    if kind == "pod":
        containers = status.get("containerStatuses") or []
        waiting_reason = None
        for container in containers:
            waiting = (container.get("state") or {}).get("waiting") or {}
            if waiting.get("reason"):
                waiting_reason = waiting["reason"]
                break
        item.update(
            {
                "phase": status.get("phase"),
                "ready": f"{sum(1 for c in containers if c.get('ready'))}/{len(containers)}",
                "restarts": sum(int(c.get("restartCount") or 0) for c in containers),
                "node": spec.get("nodeName"),
                "pod_ip": status.get("podIP"),
                "reason": waiting_reason,
            }
        )
    elif kind in ("deployment", "statefulset", "replicaset"):
        item.update(
            {
                "replicas": spec.get("replicas"),
                "ready": status.get("readyReplicas", 0),
                "available": status.get("availableReplicas", 0),
                "updated": status.get("updatedReplicas", 0),
                "images": [c.get("image") for c in (spec.get("template", {}).get("spec", {}) or {}).get("containers") or []],
            }
        )
    elif kind == "daemonset":
        item.update(
            {
                "desired": status.get("desiredNumberScheduled"),
                "ready": status.get("numberReady"),
                "available": status.get("numberAvailable"),
                "misscheduled": status.get("numberMisscheduled"),
            }
        )
    elif kind == "service":
        item.update(
            {
                "type": spec.get("type"),
                "cluster_ip": spec.get("clusterIP"),
                "ports": [
                    f"{port.get('port')}/{port.get('protocol') or 'TCP'}" for port in spec.get("ports") or []
                ],
            }
        )
    elif kind == "node":
        conditions = status.get("conditions") or []
        info = status.get("nodeInfo") or {}
        addresses = {a.get("type"): a.get("address") for a in status.get("addresses") or []}
        item.update(
            {
                "ready": next(
                    (c.get("status") == "True" for c in conditions if c.get("type") == "Ready"), False
                ),
                "kubelet_version": info.get("kubeletVersion"),
                "os_image": info.get("osImage"),
                "internal_ip": addresses.get("InternalIP"),
                "roles": sorted(
                    key.removeprefix("node-role.kubernetes.io/")
                    for key in (meta.get("labels") or {})
                    if key.startswith("node-role.kubernetes.io/")
                ),
            }
        )
    elif kind == "event":
        item.update(
            {
                "type": obj.get("type"),
                "reason": obj.get("reason"),
                "count": obj.get("count"),
                "message": str(obj.get("message") or "")[:200],
                "last_seen": obj.get("lastTimestamp") or obj.get("eventTime"),
                "object": f"{(obj.get('involvedObject') or {}).get('kind')}/{(obj.get('involvedObject') or {}).get('name')}",
            }
        )
    elif kind == "job":
        item.update(
            {
                "active": status.get("active", 0),
                "succeeded": status.get("succeeded", 0),
                "failed": status.get("failed", 0),
                "completion_time": status.get("completionTime"),
            }
        )
    elif kind == "cronjob":
        item.update({"schedule": spec.get("schedule"), "suspend": spec.get("suspend", False)})
    elif kind == "namespace":
        item.update({"phase": status.get("phase")})
    elif kind == "horizontalpodautoscaler":
        item.update(
            {
                "min_replicas": spec.get("minReplicas"),
                "max_replicas": spec.get("maxReplicas"),
                "current_replicas": status.get("currentReplicas"),
                "desired_replicas": status.get("desiredReplicas"),
            }
        )
    elif kind == "ingress":
        item.update({"hosts": [rule.get("host") for rule in spec.get("rules") or []]})
    elif kind == "persistentvolumeclaim":
        item.update(
            {
                "phase": status.get("phase"),
                "capacity": (status.get("capacity") or status.get("resources", {}).get("requests", {}) or {}).get("storage"),
                "storage_class": spec.get("storageClassName"),
            }
        )
    elif kind == "secret":
        item.update({"type": obj.get("type"), "keys": sorted((obj.get("data") or {}).keys())})
    elif kind == "configmap":
        item.update({"keys": sorted((obj.get("data") or {}).keys())})
    return {key: value for key, value in item.items() if value is not None}


def _mock_k8s(kind: str, namespace: str) -> dict[str, Any]:
    ns = namespace or "prod"
    created = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pods = [
        {"name": "checkout-6d4f7c8b9d-x2k4p", "namespace": ns, "created_at": created, "age": "3d",
         "phase": "Running", "ready": "1/1", "restarts": 0, "node": "cn-hangzhou.10.0.3.21"},
        {"name": "checkout-6d4f7c8b9d-m8zqw", "namespace": ns, "created_at": created, "age": "3d",
         "phase": "Running", "ready": "1/1", "restarts": 7, "node": "cn-hangzhou.10.0.3.22"},
        {"name": "payment-5b7d9c6f4-q4tnz", "namespace": ns, "created_at": created, "age": "12h",
         "phase": "CrashLoopBackOff", "ready": "0/1", "restarts": 23, "node": "cn-hangzhou.10.0.3.23",
         "reason": "CrashLoopBackOff"},
    ]
    deployments = [
        {"name": "checkout", "namespace": ns, "created_at": created, "age": "3d",
         "replicas": 3, "ready": 3, "available": 3, "updated": 3},
        {"name": "payment", "namespace": ns, "created_at": created, "age": "3d",
         "replicas": 2, "ready": 0, "available": 0, "updated": 2},
    ]
    nodes = [
        {"name": "cn-hangzhou.10.0.3.21", "created_at": created, "age": "30d", "ready": True,
         "kubelet_version": "v1.30.2", "internal_ip": "10.0.3.21", "roles": ["worker"]},
        {"name": "cn-hangzhou.10.0.3.22", "created_at": created, "age": "30d", "ready": False,
         "kubelet_version": "v1.30.2", "internal_ip": "10.0.3.22", "roles": ["worker"]},
    ]
    events = [
        {"name": "payment-5b7d9c6f4-q4tnz.17f2a1", "namespace": ns, "age": "8m", "type": "Warning",
         "reason": "BackOff", "count": 42, "message": "Back-off restarting failed container",
         "object": "Pod/payment-5b7d9c6f4-q4tnz"},
        {"name": "checkout-6d4f7c8b9d.17f2a2", "namespace": ns, "age": "1h", "type": "Normal",
         "reason": "ScalingReplicaSet", "count": 1, "message": "Scaled up replica set checkout-6d4f7c8b9d to 3",
         "object": "Deployment/checkout"},
    ]
    samples: dict[str, list[dict[str, Any]]] = {
        "pod": pods,
        "deployment": deployments,
        "node": nodes,
        "event": events,
    }
    items = samples.get(kind, [
        {"name": f"{kind}-demo-0", "namespace": ns, "created_at": created, "age": "3d"},
        {"name": f"{kind}-demo-1", "namespace": ns, "created_at": created, "age": "3d"},
    ])
    return {"kind": kind, "namespace": namespace, "count": len(items), "items": items[:_MAX_ITEMS]}


@tool
async def k8s_list_resources(kind: str, namespace: str = "") -> str:
    """列出 Kubernetes 集群资源, 用于查看资源状态与异常事件。

    kind 支持: pod / deployment / statefulset / daemonset / replicaset / service /
    configmap / secret / serviceaccount / event / job / cronjob / ingress / node /
    namespace / persistentvolume / persistentvolumeclaim / horizontalpodautoscaler
    (复数与常见缩写同样可识别, 如 pods、hpa、svc)。
    namespace 为空表示全部命名空间(集群级资源会忽略该参数)。
    返回 JSON: items[] 是该类资源的关键字段(如 Pod 的 phase/ready/restarts/node)。
    """
    canonical = _normalize_kind(kind)
    if canonical is None:
        return _dumps(
            {
                "source": "error",
                "connector": "kubernetes",
                "error": f"不支持的资源类型: {kind!r}",
                "supported": sorted(_K8S_KINDS),
            }
        )

    settings = _settings()
    base = settings.k8s_api_url.strip().rstrip("/")
    spec = _K8S_KINDS[canonical]
    ns = namespace.strip()
    if not base:
        return _dumps(
            _mock_or_unavailable(
                "kubernetes", "未配置 K8S_API_URL", _mock_k8s(canonical, ns)
            )
        )

    if spec.namespaced and ns:
        url = f"{base}{spec.api}/namespaces/{ns}/{spec.plural}"
    else:
        url = f"{base}{spec.api}/{spec.plural}"
    try:
        payload = await _get_json(
            url,
            params={"limit": _MAX_ITEMS},
            headers={**_bearer(settings.k8s_api_token), "Accept": "application/json"},
            verify=settings.k8s_verify_tls,
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Kubernetes 调用失败, 回退演示数据: %s", exc)
        return _dumps(
            _mock_or_unavailable("kubernetes", f"调用失败: {exc}", _mock_k8s(canonical, ns))
        )

    if payload.get("kind") == "Status" and payload.get("status") == "Failure":
        return _dumps(_query_error("kubernetes", payload, {"kind": canonical, "namespace": ns}))

    raw_items = payload.get("items") or []
    items = [_summarize(canonical, obj) for obj in raw_items[:_MAX_ITEMS]]
    out: dict[str, Any] = {
        "source": "kubernetes",
        "kind": canonical,
        "namespace": ns,
        "count": len(raw_items),
        "returned": len(items),
        "items": items,
    }
    if len(raw_items) > _MAX_ITEMS:
        out["truncated"] = True
    return _dumps(out)


#: 运维连接器工具集合; 由 ``app/tools.py`` 并入 ``ALL_TOOLS``(本地工具目录)。
OPS_TOOLS = [prometheus_query, loki_query, k8s_list_resources]
