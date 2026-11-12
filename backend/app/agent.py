"""Agent 智能体：LangChain 工具注册 + 意图识别 + 工具调用链编排 + 记忆。

按 PRD 2.1.3：自动调度工具、主动拉取运维数据、复杂故障拆解为多步骤执行，
工具调用失败有降级策略，调用链全程可追溯。
本版按领域守卫给出的 intent/focus 精准选工具，避免任何问题都拉全量数据。
"""
import asyncio
import json
import re

from langchain_core.tools import tool

from app import datasources as ds
from app.deps import get_redis


@tool
async def query_metrics_tool(server: str) -> str:
    """查询指定服务器的 Prometheus 监控指标。"""
    return json.dumps(await ds.fetch_metrics(server), ensure_ascii=False)


@tool
async def search_logs_tool(server: str, keyword: str = "") -> str:
    """检索指定服务器近期日志，可按关键词过滤。"""
    logs = await ds.search_logs(server, keyword=keyword)
    return json.dumps(logs, ensure_ascii=False)


@tool
async def query_trace_tool(server: str) -> str:
    """查询指定服务器的请求链路追踪数据。"""
    return json.dumps(await ds.fetch_trace(server), ensure_ascii=False)


@tool
async def server_info_tool(server: str) -> str:
    """查询服务器基础信息与当前健康状态。"""
    return json.dumps(await ds.server_info(server), ensure_ascii=False)


TOOL_REGISTRY = {
    "metrics": query_metrics_tool,
    "logs": search_logs_tool,
    "trace": query_trace_tool,
    "server": server_info_tool,
}


def extract_server(question: str) -> str:
    match = re.search(r"(\d{3,6})\s*(?:号)?服务器|服务器\s*(\d{3,6})", question)
    if match:
        return match.group(1) or match.group(2)
    match = re.search(r"\b(\d{3,6})\b", question)
    return match.group(1) if match else "0931"


def select_tools(question: str, permissions: set[str], intent: str = "diagnose") -> list[str]:
    """按意图 + 数据级权限决定要调用的工具，不再对任何问句都拉全量数据。"""
    wants = {
        "logs": ["logs", "server"],
        "trace": ["trace", "server"],
        "metrics": ["server", "metrics"],
        "server_info": ["server", "metrics"],
        "suggestion": ["server", "metrics", "logs"],
        "diagnose": ["server", "metrics", "logs", "trace"],
    }.get(intent, ["server", "metrics", "logs", "trace"])

    tools = []
    for name in wants:
        if name == "logs" and "logs.view" not in permissions:
            continue
        if name == "trace" and "trace.view" not in permissions:
            continue
        if name == "metrics" and not ({"metrics.view", "metrics.overview"} & permissions):
            continue
        if name not in tools:
            tools.append(name)
    return tools or ["server", "metrics"]


async def _invoke(name: str, server: str, question: str) -> dict:
    tool_obj = TOOL_REGISTRY[name]
    try:
        args = {"server": server}
        if name == "logs":
            args["keyword"] = ""
        raw = await tool_obj.ainvoke(args)
        data = json.loads(raw) if isinstance(raw, str) else raw
        return {"tool": name, "status": "ok", "data": data}
    except Exception as exc:
        return {
            "tool": name,
            "status": "fallback",
            "error": f"{type(exc).__name__}: {exc}"[:160],
            "data": None,
        }


async def conversation_context(conversation_id: int) -> list[dict]:
    """多轮对话记忆：Redis 优先，回落 PostgreSQL 由路由层保证。"""
    r = get_redis()
    key = f"opsai:conv:{conversation_id}"
    try:
        raw = await r.lrange(key, -8, -1)
        return [json.loads(x) for x in raw]
    except Exception:
        return []


async def append_context(conversation_id: int, role: str, content: str) -> None:
    r = get_redis()
    key = f"opsai:conv:{conversation_id}"
    try:
        await r.rpush(key, json.dumps({"role": role, "content": content[:500]}, ensure_ascii=False))
        await r.expire(key, 3600)
    except Exception:
        pass


async def investigate(question: str, permissions: set[str], conversation_id: int = 0,
                      guard: dict | None = None) -> dict:
    """执行完整排障链：领域守卫 → 精准选工具 → 并行调用 → 多源证据 → 针对性分析。"""
    guard = guard or {}
    intent = guard.get("intent", "diagnose")
    focus = guard.get("focus")

    server = guard.get("server") or extract_server(question)
    server_explicit = bool(guard.get("has_server"))

    history = await conversation_context(conversation_id) if conversation_id else []
    # 追问时关联上一轮上下文（PRD 验收标准）
    if (guard.get("is_followup") or (history and re.search(r"那|再|还有|继续|为什么|具体|详情|哪个", question))):
        prev_servers = [
            extract_server(h.get("content", "")) for h in history if h.get("role") == "user"
        ]
        if prev_servers and not re.search(r"\d{3,6}", question):
            server = prev_servers[-1]
            server_explicit = True

    planned = select_tools(question, permissions, intent)
    results = await asyncio.gather(*[_invoke(t, server, question) for t in planned])

    evidence = {"server": server, "server_explicit": server_explicit,
                "metrics": {}, "logs": [], "trace": {}, "question": question}
    trace = []
    for item in results:
        trace.append({"tool": item["tool"], "status": item["status"], "error": item.get("error")})
        if item["status"] != "ok" or item["data"] is None:
            continue
        if item["tool"] == "metrics":
            evidence["metrics"] = item["data"]
        elif item["tool"] == "logs":
            evidence["logs"] = item["data"]
        elif item["tool"] == "trace":
            evidence["trace"] = item["data"]
        elif item["tool"] == "server":
            evidence["server_info"] = item["data"]

    # 数据级权限过滤：无 logs.view / trace.view 的角色只拿到概览
    if "logs.view" not in permissions:
        evidence["raw_logs_hidden"] = True
        evidence["logs"] = []
    if "trace.view" not in permissions:
        evidence["raw_trace_hidden"] = True
        evidence["trace"] = {}

    from app.llm import analyze

    conclusion = await analyze(question, evidence, intent, focus)
    conclusion["server"] = server
    conclusion["server_explicit"] = server_explicit
    conclusion["tool_trace"] = trace
    conclusion["intent"] = intent
    conclusion["focus"] = focus
    conclusion["data_scope"] = (
        "full" if "logs.view" in permissions else "overview_only"
    )
    return conclusion
