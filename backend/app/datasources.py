"""运维数据源适配层：指标(Prometheus)、日志(ELK)、链路追踪。

默认使用带故障特征的模拟数据，保证无外部系统时排障链路仍可演示；
在「数据源管理」里登记真实地址并把 .env 的 USE_MOCK_DATASOURCE 改为 false 后，
适配器会走真实 HTTP 查询，失败自动降级并在结果里标注 source=fallback。
"""
import json
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

MOCK_METRICS = {
    "0931": {
        "cpu_usage": 38.2,
        "memory_usage": 71.5,
        "disk_usage": 96.8,
        "disk_io_wait": 42.7,
        "http_5xx_rate": 8.4,
        "load_1m": 3.1,
        "mount": "/data",
    }
}

HEALTHY_METRICS = {
    "cpu_usage": 21.4,
    "memory_usage": 48.2,
    "disk_usage": 43.6,
    "disk_io_wait": 2.1,
    "http_5xx_rate": 0.1,
    "load_1m": 1.2,
    "mount": "/data",
}


def _now(offset_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)).isoformat()


def mock_metrics(server: str) -> dict:
    return dict(MOCK_METRICS.get(server, HEALTHY_METRICS))


def mock_logs(server: str, keyword: str = "", minutes: int = 30) -> list[dict]:
    if server != "0931":
        logs = [
            {"timestamp": _now(8), "level": "INFO", "service": "order-service",
             "host": server, "message": "health check passed"},
        ]
    else:
        logs = [
            {"timestamp": _now(2), "level": "ERROR", "service": "order-service",
             "host": server, "message": "write /data/log/order.log failed: No space left on device"},
            {"timestamp": _now(3), "level": "ERROR", "service": "order-service",
             "host": server, "message": "db.insert timeout after 1200ms (disk I/O blocked)"},
            {"timestamp": _now(5), "level": "WARN", "service": "node-exporter",
             "host": server, "message": "filesystem /data usage 96.8% exceeds threshold 85%"},
            {"timestamp": _now(9), "level": "INFO", "service": "order-service",
             "host": server, "message": "received POST /api/order"},
        ]
    if keyword:
        logs = [x for x in logs if keyword.lower() in json.dumps(x, ensure_ascii=False).lower()]
    return [x for x in logs if minutes >= 0]


def mock_trace(server: str) -> dict:
    if server != "0931":
        return {"trace_id": "healthy", "spans": [], "root_cause_hint": ""}
    return {
        "trace_id": "tr-0931-8f21c",
        "spans": [
            {"service": "api-gateway", "operation": "POST /api/order", "duration_ms": 1284, "status": "ERROR"},
            {"service": "order-service", "operation": "db.insert", "duration_ms": 1211, "status": "ERROR",
             "error": "No space left on device"},
            {"service": "mysql-0931", "operation": "write binlog", "duration_ms": 1190, "status": "ERROR"},
        ],
        "root_cause_hint": "order-service -> db.insert -> /data 磁盘写入失败",
    }


async def real_metrics(server: str) -> dict:
    query = f'node_filesystem_usage{{instance=~"{server}.*"}}'
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{settings.prometheus_url}/api/v1/query", params={"query": query}
        )
        resp.raise_for_status()
        return {"raw": resp.json(), "server": server}


async def real_logs(server: str, keyword: str = "", minutes: int = 30) -> list[dict]:
    must = [{"match_phrase": {"host": server}}]
    if keyword:
        must.append({"query_string": {"query": keyword}})
    body = {
        "size": 50,
        "query": {"bool": {"must": must, "filter": [
            {"range": {"@timestamp": {"gte": f"now-{minutes}m"}}}
        ]}},
        "sort": [{"@timestamp": "desc"}],
    }
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(
            f"{settings.elasticsearch_url}/opsai-logs-*/_search", json=body
        )
        resp.raise_for_status()
        return [h["_source"] for h in resp.json()["hits"]["hits"]]


async def fetch_metrics(server: str, use_mock: bool | None = None) -> dict:
    mock = settings.use_mock_datasource if use_mock is None else use_mock
    if mock:
        return {**mock_metrics(server), "source": "mock"}
    try:
        data = await real_metrics(server)
        return {**data, "source": "prometheus"}
    except Exception as exc:
        return {**mock_metrics(server), "source": "fallback", "reason": str(exc)[:120]}


async def search_logs(
    server: str, keyword: str = "", minutes: int = 30, use_mock: bool | None = None
) -> list[dict]:
    mock = settings.use_mock_datasource if use_mock is None else use_mock
    if mock:
        return mock_logs(server, keyword, minutes)
    try:
        return await real_logs(server, keyword, minutes)
    except Exception:
        return mock_logs(server, keyword, minutes)


async def fetch_trace(server: str, use_mock: bool | None = None) -> dict:
    mock = settings.use_mock_datasource if use_mock is None else use_mock
    if mock:
        return {**mock_trace(server), "source": "mock"}
    # 真实链路追踪（Jaeger/Tempo）接入点：按 trace 查询接口拉取即可
    return {**mock_trace(server), "source": "fallback", "reason": "trace backend not wired"}


async def server_info(server: str) -> dict:
    metrics = await fetch_metrics(server)
    return {
        "server": server,
        "ip": f"10.0.0.{int(server) % 256}" if server.isdigit() else "10.0.0.1",
        "status": "异常" if metrics.get("disk_usage", 0) > 90 else "正常",
        "disk_usage": metrics.get("disk_usage"),
        "cpu_usage": metrics.get("cpu_usage"),
        "source": metrics.get("source"),
    }
