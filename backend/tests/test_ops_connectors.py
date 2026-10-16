"""运维连接器工具(Prometheus / Loki / Kubernetes)的三条路径。

覆盖点:
- **未接入**: env 端点为空 → 结构化演示数据(``source="mock"``), 保证无凭证也能跑通链路;
- **已接入**: monkeypatch ``_get_json`` → 真实解析路径(URL/参数/鉴权头/截断/字段压缩);
- **失败回退**: 请求抛错 → mock; ``OPS_MOCK_FALLBACK=false`` → ``source="unavailable"``;
- 端点可达但查询被拒(PromQL 语法错误 / K8s Failure Status)→ 原样回传 ``source="error"``,
  不拿演示数据掩盖真实错误。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app import tools_ops
from app.config import Settings
from app.tools import ALL_TOOLS
from app.tools_ops import k8s_list_resources, loki_query, prometheus_query

CONNECTOR_NAMES = {"prometheus_query", "loki_query", "k8s_list_resources"}


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """注入一份受控 Settings(conftest 已冻结其余环境变量)。"""
    settings = Settings(**overrides)
    monkeypatch.setattr(tools_ops, "_settings", lambda: settings)
    return settings


def _stub_http(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """替换真实 HTTP 层, 记录每次调用的 url/params/headers/verify。"""
    calls: list[dict[str, Any]] = []

    async def fake_get_json(url, *, params=None, headers=None, verify=True):
        calls.append({"url": url, "params": params, "headers": headers, "verify": verify})
        return payload

    monkeypatch.setattr(tools_ops, "_get_json", fake_get_json)
    return calls


def _raise_http(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise exc

    monkeypatch.setattr(tools_ops, "_get_json", boom)


# ---------------------------------------------------------------------------
# 目录注册
# ---------------------------------------------------------------------------


def test_ops_tools_are_part_of_local_catalog() -> None:
    names = {tool.name for tool in ALL_TOOLS}
    assert CONNECTOR_NAMES <= names
    assert names == {"get_current_time", "calculate", "get_weather", *CONNECTOR_NAMES}
    assert [tool.name for tool in tools_ops.OPS_TOOLS] == [
        "prometheus_query",
        "loki_query",
        "k8s_list_resources",
    ]


# ---------------------------------------------------------------------------
# 未接入 → mock
# ---------------------------------------------------------------------------


async def test_prometheus_returns_mock_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, prometheus_url="")
    data = json.loads(await prometheus_query.ainvoke({"promql": "up", "minutes": 30}))

    assert data["source"] == "mock"
    assert data["connector"] == "prometheus"
    assert "PROMETHEUS_URL" in data["reason"]
    assert data["query"] == "up"
    assert data["window_minutes"] == 30
    assert data["series"], "演示数据必须给出至少一条时序"
    for series in data["series"]:
        assert set(series) >= {"metric", "points", "latest"}
        for point in series["points"]:
            assert isinstance(point, list) and len(point) == 2


async def test_loki_returns_mock_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, loki_url="")
    data = json.loads(await loki_query.ainvoke({"logql": '{ns="prod"} |= "error"'}))

    assert data["source"] == "mock"
    assert data["connector"] == "loki"
    assert data["stream_count"] >= 1
    lines = [line for stream in data["streams"] for line in stream["lines"]]
    assert lines and all(isinstance(line[1], str) for line in lines)
    assert any("ERROR" in line[1] for line in lines), "演示日志需含异常样本, 否则无法联调日志分析智能体"


async def test_k8s_returns_mock_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, k8s_api_url="")
    data = json.loads(await k8s_list_resources.ainvoke({"kind": "pod", "namespace": "prod"}))

    assert data["source"] == "mock"
    assert data["connector"] == "kubernetes"
    assert data["kind"] == "pod"
    assert data["count"] == len(data["items"]) >= 1
    phases = {item["phase"] for item in data["items"]}
    assert "Running" in phases


# ---------------------------------------------------------------------------
# 已接入 → 真实解析路径
# ---------------------------------------------------------------------------


async def test_prometheus_real_path_truncates_and_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, prometheus_url="http://prom.example:9090/", prometheus_token="tk")
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"pod": f"app-{i}"},
                    "values": [[1_700_000_000 + step * 60, f"0.{step}"] for step in range(40)],
                }
                for i in range(10)
            ],
        },
    }
    calls = _stub_http(monkeypatch, payload)

    data = json.loads(await prometheus_query.ainvoke({"promql": "up", "minutes": 60}))
    call = calls[0]

    assert call["url"] == "http://prom.example:9090/api/v1/query_range"
    assert call["params"]["query"] == "up"
    assert call["params"]["step"] == 30  # 60 分钟 → 60*60//120
    assert call["params"]["end"] - call["params"]["start"] == 3600
    assert call["headers"] == {"Authorization": "Bearer tk"}

    assert data["source"] == "prometheus"
    assert data["series_count"] == 10
    assert len(data["series"]) == tools_ops._MAX_SERIES
    assert data["truncated_series"] == 10 - tools_ops._MAX_SERIES
    # 只保留最近的 N 个点, 且时间戳/数值被规整成数字
    assert len(data["series"][0]["points"]) == tools_ops._MAX_POINTS_PER_SERIES
    assert data["series"][0]["points"][-1][0] == 1_700_000_000 + 39 * 60
    assert data["series"][0]["latest"] == 0.39


async def test_prometheus_handles_instant_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, prometheus_url="http://prom:9090")
    _stub_http(
        monkeypatch,
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"job": "node"}, "value": [1_700_000_000, "3"]}],
            },
        },
    )

    data = json.loads(await prometheus_query.ainvoke({"promql": "up", "minutes": 5}))

    assert data["result_type"] == "vector"
    assert data["series"][0]["points"] == [[1_700_000_000, 3]]
    assert data["series"][0]["latest"] == 3


async def test_loki_real_path_normalizes_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, loki_url="http://loki:3100", loki_token="tk2")
    ns = int(datetime(2026, 10, 16, tzinfo=UTC).timestamp() * 1e9)
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"namespace": "prod", "pod": "app-1"},
                    "values": [
                        [str(ns), "boom"],
                        [str(ns - 1_000_000_000), "earlier"],
                        "malformed-entry",
                    ],
                },
                {"stream": {"pod": "app-2"}, "values": [[str(ns), "second stream"]]},
            ],
        },
    }
    calls = _stub_http(monkeypatch, payload)

    data = json.loads(await loki_query.ainvoke({"logql": '{ns="prod"}', "limit": 10, "minutes": 15}))
    call = calls[0]

    assert call["url"] == "http://loki:3100/loki/api/v1/query_range"
    assert call["params"]["limit"] == 10
    assert call["params"]["direction"] == "backward"
    assert call["params"]["end"] - call["params"]["start"] == 15 * 60 * 1_000_000_000
    assert call["headers"] == {"Authorization": "Bearer tk2"}

    assert data["source"] == "loki"
    assert data["stream_count"] == 2
    assert data["line_count"] == 3
    first = data["streams"][0]
    assert first["labels"] == {"namespace": "prod", "pod": "app-1"}
    assert first["lines"][0][0].startswith("2026-10-16T00:00:00")
    assert first["lines"][0][1] == "boom"


async def test_loki_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, loki_url="http://loki:3100")
    ns = str(int(datetime(2026, 10, 16, tzinfo=UTC).timestamp() * 1e9))
    _stub_http(
        monkeypatch,
        {
            "status": "success",
            "data": {
                "result": [
                    {"stream": {"pod": "a"}, "values": [[ns, f"line-{i}"] for i in range(30)]},
                    {"stream": {"pod": "b"}, "values": [[ns, "late"]]},
                ]
            },
        },
    )

    data = json.loads(await loki_query.ainvoke({"logql": "{pod=~'.+'}", "limit": 5}))

    assert data["line_count"] == 5
    assert data["stream_count"] == 1
    assert data["truncated"] is True


async def test_k8s_real_path_builds_namespaced_url_and_summarizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, k8s_api_url="https://k8s.example", k8s_api_token="sa", k8s_verify_tls=False)
    created = (datetime.now(UTC) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "kind": "PodList",
        "items": [
            {
                "metadata": {"name": "checkout-1", "namespace": "prod", "creationTimestamp": created},
                "spec": {"nodeName": "node-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.1.2.3",
                    "containerStatuses": [
                        {"ready": True, "restartCount": 2},
                        {
                            "ready": False,
                            "restartCount": 5,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        },
                    ],
                },
            }
        ],
    }
    calls = _stub_http(monkeypatch, payload)

    data = json.loads(await k8s_list_resources.ainvoke({"kind": "pods", "namespace": "prod"}))
    call = calls[0]

    assert call["url"] == "https://k8s.example/api/v1/namespaces/prod/pods"
    assert call["headers"]["Authorization"] == "Bearer sa"
    assert call["headers"]["Accept"] == "application/json"
    assert call["verify"] is False

    assert data["source"] == "kubernetes"
    assert data["kind"] == "pod"
    assert data["count"] == 1
    item = data["items"][0]
    assert item["name"] == "checkout-1"
    assert item["phase"] == "Running"
    assert item["ready"] == "1/2"
    assert item["restarts"] == 7
    assert item["reason"] == "CrashLoopBackOff"
    assert item["node"] == "node-a"
    assert item["age"].endswith("h")


async def test_k8s_cluster_scoped_kind_ignores_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, k8s_api_url="https://k8s.example")
    _stub_http(monkeypatch, {"kind": "NodeList", "items": []})

    calls = _stub_http(monkeypatch, {"kind": "NodeList", "items": []})
    data = json.loads(await k8s_list_resources.ainvoke({"kind": "Node", "namespace": "prod"}))

    assert calls[0]["url"] == "https://k8s.example/api/v1/nodes"
    assert data["namespace"] == "prod"  # 回显请求参数, 但不影响 URL
    assert data["items"] == []


async def test_k8s_kind_aliases_resolve_to_group_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, k8s_api_url="https://k8s.example")
    base = "https://k8s.example"
    cases = {
        "deploy": "/apis/apps/v1/namespaces/prod/deployments",
        "hpa": "/apis/autoscaling/v2/namespaces/prod/horizontalpodautoscalers",
        "svc": "/api/v1/namespaces/prod/services",
        "ingresses": "/apis/networking.k8s.io/v1/namespaces/prod/ingresses",
        "CronJobs": "/apis/batch/v1/namespaces/prod/cronjobs",
        "Pods": "/api/v1/namespaces/prod/pods",
    }
    for kind, expected in cases.items():
        calls = _stub_http(monkeypatch, {"kind": "List", "items": []})
        await k8s_list_resources.ainvoke({"kind": kind, "namespace": "prod"})
        assert calls[0]["url"] == base + expected, kind


async def test_k8s_deployment_summary_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, k8s_api_url="https://k8s.example")
    _stub_http(
        monkeypatch,
        {
            "kind": "DeploymentList",
            "items": [
                {
                    "metadata": {"name": "payment", "namespace": "prod"},
                    "spec": {
                        "replicas": 3,
                        "template": {"spec": {"containers": [{"image": "payment:1.4.2"}]}},
                    },
                    "status": {"readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 3},
                }
            ],
        },
    )

    data = json.loads(await k8s_list_resources.ainvoke({"kind": "deployment", "namespace": "prod"}))
    item = data["items"][0]

    assert item["replicas"] == 3
    assert item["ready"] == 1
    assert item["images"] == ["payment:1.4.2"]
    assert "namespace" in item


# ---------------------------------------------------------------------------
# 失败与降级
# ---------------------------------------------------------------------------


async def test_unknown_kind_reports_supported_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, k8s_api_url="https://k8s.example")

    data = json.loads(await k8s_list_resources.ainvoke({"kind": "fluxcd-helmrelease"}))

    assert data["source"] == "error"
    assert "fluxcd-helmrelease" in data["error"]
    assert "pod" in data["supported"] and "deployment" in data["supported"]


async def test_connection_failure_falls_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, prometheus_url="http://prom:9090", loki_url="http://loki:3100", k8s_api_url="https://k8s")
    _raise_http(monkeypatch, httpx.ConnectError("connection refused"))

    prom = json.loads(await prometheus_query.ainvoke({"promql": "up"}))
    loki = json.loads(await loki_query.ainvoke({"logql": '{a="b"}'}))
    k8s = json.loads(await k8s_list_resources.ainvoke({"kind": "pod"}))

    for data in (prom, loki, k8s):
        assert data["source"] == "mock"
        assert "调用失败" in data["reason"]
        assert data["note"] == tools_ops._MOCK_NOTE


async def test_mock_fallback_disabled_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, prometheus_url="", ops_mock_fallback=False)
    data = json.loads(await prometheus_query.ainvoke({"promql": "up"}))

    assert data == {
        "source": "unavailable",
        "connector": "prometheus",
        "reason": "未配置 PROMETHEUS_URL",
    }


async def test_query_syntax_error_is_not_masked_by_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, prometheus_url="http://prom:9090", loki_url="http://loki:3100")
    _stub_http(
        monkeypatch, {"status": "error", "errorType": "bad_data", "error": "unexpected token"}
    )

    prom = json.loads(await prometheus_query.ainvoke({"promql": "up{"}))

    assert prom["source"] == "error"
    assert prom["error"] == "unexpected token"
    assert prom["query"] == "up{"


async def test_k8s_failure_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, k8s_api_url="https://k8s.example")
    _stub_http(
        monkeypatch,
        {"kind": "Status", "status": "Failure", "message": 'pods is forbidden: User cannot list'},
    )

    data = json.loads(await k8s_list_resources.ainvoke({"kind": "pod", "namespace": "kube-system"}))

    assert data["source"] == "error"
    assert "forbidden" in data["error"]


async def test_window_and_limit_are_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)  # 端点全空 → mock 路径也会回显生效窗口
    prom = json.loads(await prometheus_query.ainvoke({"promql": "up", "minutes": 999_999}))
    loki = json.loads(await loki_query.ainvoke({"logql": "{a='b'}", "limit": 999_999}))

    assert prom["window_minutes"] == 24 * 60
    assert loki["line_count"] <= tools_ops._MAX_LOG_LINES
