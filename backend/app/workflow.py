"""自研轻量工作流引擎：JSON 定义流程节点 + 状态机驱动执行。

支持顺序执行、条件分支、并行执行、异常处理（PRD 3.8 / 2.1.5）。
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

ACTION_HANDLERS: dict[str, Any] = {}


def action(name: str):
    def decorator(fn):
        ACTION_HANDLERS[name] = fn
        return fn
    return decorator


@action("query_server")
async def _query_server(context: dict, params: dict) -> dict:
    from app.datasources import fetch_metrics
    metrics = await fetch_metrics(params.get("server", context.get("server", "0931")))
    return {"disk_usage": metrics.get("disk_usage"), "source": metrics.get("source")}


@action("clean_disk")
async def _clean_disk(context: dict, params: dict) -> dict:
    # 真实环境此处调用 Ansible playbook；本地演示返回模拟执行结果
    released = params.get("released_gb", 12.5)
    context["disk_released_gb"] = released
    return {"released_gb": released, "via": "ansible(模拟)"}


@action("restart_service")
async def _restart_service(context: dict, params: dict) -> dict:
    return {"service": params.get("service", "order-service"), "restarted": True, "via": "ansible(模拟)"}


@action("notify")
async def _notify(context: dict, params: dict) -> dict:
    return {"channel": params.get("channel", "wecom"), "message": params.get("message", "")}


_COND = re.compile(r"^\s*([\w.]+)\s*(>=|<=|==|!=|>|<)\s*([\w.]+)\s*$")


def evaluate_condition(expression: str, context: dict) -> bool:
    m = _COND.match(expression or "")
    if not m:
        return False
    key, op, raw = m.groups()
    left = context.get(key)
    try:
        right = float(raw) if "." in raw else int(raw)
    except ValueError:
        right = raw.strip("'\"")
    try:
        if op == ">": return float(left) > float(right)
        if op == "<": return float(left) < float(right)
        if op == ">=": return float(left) >= float(right)
        if op == "<=": return float(left) <= float(right)
        if op == "==": return left == right
        if op == "!=": return left != right
    except (TypeError, ValueError):
        return False
    return False


class WorkflowEngine:
    def __init__(self, definition: dict):
        self.nodes = {n["id"]: n for n in definition.get("nodes", [])}
        self.start = definition.get("start", "start")

    async def _exec_action(self, node: dict, context: dict) -> dict:
        name = node.get("action", "")
        handler = ACTION_HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"未注册的自动化动作: {name}")
        return await handler(context, node.get("params", {}))

    async def run(self, context: dict | None = None, max_steps: int = 30) -> dict:
        ctx = dict(context or {})
        log: list[dict] = []
        current = self.start
        status = "succeeded"
        steps = 0

        while current and current != "end" and steps < max_steps:
            steps += 1
            node = self.nodes.get(current)
            if node is None:
                status = "failed"
                log.append({"node": current, "error": "节点不存在"})
                break
            ntype = node.get("type", "action")
            entry = {"node": current, "type": ntype, "at": datetime.now(timezone.utc).isoformat()}
            try:
                if ntype == "start":
                    current = node.get("next")
                elif ntype == "action":
                    result = await self._exec_action(node, ctx)
                    entry["result"] = result
                    if isinstance(result, dict):
                        ctx.update({k: v for k, v in result.items() if k not in ctx})
                    current = node.get("next")
                elif ntype == "condition":
                    hit = evaluate_condition(node.get("expression", ""), ctx)
                    entry["expression"] = node.get("expression")
                    entry["matched"] = hit
                    branches = node.get("branches", {})
                    current = branches.get("true" if hit else "false") or node.get("next")
                elif ntype == "parallel":
                    children = [
                        self._exec_action(self.nodes[cid], ctx)
                        for cid in node.get("nodes", []) if cid in self.nodes
                    ]
                    results = await asyncio.gather(*children, return_exceptions=True)
                    entry["results"] = [
                        r if not isinstance(r, Exception) else f"error: {r}" for r in results
                    ]
                    current = node.get("next")
                elif ntype == "end":
                    current = None
                else:
                    raise ValueError(f"未知节点类型: {ntype}")
                entry["status"] = "ok"
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                status = "failed"
                current = node.get("on_error")
                log.append(entry)
                if not current:
                    break
                continue
            log.append(entry)

        if steps >= max_steps and current not in (None, "end"):
            status = "failed"
            log.append({"error": "超过最大步数，疑似循环"})
        return {
            "status": status,
            "current_node": current or "end",
            "context": ctx,
            "log": log,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
