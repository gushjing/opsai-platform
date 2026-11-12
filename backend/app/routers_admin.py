"""运维管理接口：数据源、服务器查询/操控、工作流、审计、报表。"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import datasources as ds
from app.db import get_db
from app.deps import (
    current_user,
    get_redis,
    permission_codes,
    require_permission,
    write_audit,
)
from app.models import AuditLog, Conversation, DataSource, Message, User, WorkflowDef, WorkflowRun
from app.timeutil import fmt_dt, fmt_iso, now_local
from app.workflow import WorkflowEngine

router = APIRouter(prefix="/api", tags=["运维管理"])


class DataSourceIn(BaseModel):
    name: str
    ds_type: str  # prometheus / elk / trace
    url: str = ""
    config: dict = {}
    enabled: bool = True


class ServerIn(BaseModel):
    server: str


class ExecuteIn(BaseModel):
    server: str
    command: str
    confirm_token: str | None = None


class WorkflowIn(BaseModel):
    name: str
    definition: dict


class WorkflowRunIn(BaseModel):
    context: dict = {}


DANGEROUS = ["rm -rf /", "mkfs", ":(){", "shutdown -h", "dd if=/dev/zero"]


# ---------- 数据源配置（管理员） ----------

@router.post("/datasources", status_code=201)
async def create_datasource(
    body: DataSourceIn,
    request: Request,
    user: User = Depends(require_permission("datasource.manage")),
    db: AsyncSession = Depends(get_db),
):
    exists = (await db.execute(select(DataSource).where(DataSource.name == body.name))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "数据源名称已存在")
    item = DataSource(
        name=body.name, ds_type=body.ds_type, url=body.url,
        config_json=json.dumps(body.config, ensure_ascii=False), enabled=body.enabled,
    )
    db.add(item)
    await db.commit()
    await write_audit(db, user, "datasource.create", target=body.name,
                      detail={"type": body.ds_type, "url": body.url},
                      ip=request.client.host if request.client else "")
    return {"id": item.id, **body.model_dump()}


@router.get("/datasources")
async def list_datasources(
    user: User = Depends(require_permission("datasource.manage")),
    db: AsyncSession = Depends(get_db),
):
    items = (await db.execute(select(DataSource).order_by(DataSource.id))).scalars().all()
    return [{"id": i.id, "name": i.name, "ds_type": i.ds_type, "url": i.url,
             "config": json.loads(i.config_json), "enabled": i.enabled} for i in items]


@router.delete("/datasources/{item_id}")
async def delete_datasource(
    item_id: int, request: Request,
    user: User = Depends(require_permission("datasource.manage")),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(DataSource, item_id)
    if item is None:
        raise HTTPException(404, "数据源不存在")
    await db.delete(item)
    await db.commit()
    await write_audit(db, user, "datasource.delete", target=item.name,
                      ip=request.client.host if request.client else "")
    return {"deleted": item_id}


# ---------- 运维数据查询（数据级权限过滤） ----------

@router.post("/ops/query")
async def query_server(
    body: ServerIn,
    user: User = Depends(current_user),
):
    info = await ds.server_info(body.server)
    perms = permission_codes(user)
    if "metrics.view" not in perms:
        # 普通用户 / 领导仅可见概览（PRD 权限矩阵）
        info = {
            "server": info["server"], "status": info["status"],
            "disk_usage": info["disk_usage"], "scope": "overview",
        }
    else:
        info = {**info, **await ds.fetch_metrics(body.server), "scope": "full"}
    return info


@router.post("/ops/logs")
async def query_logs(
    body: ServerIn, keyword: str = "", minutes: int = 30,
    user: User = Depends(require_permission("logs.view")),
):
    return {"server": body.server, "logs": await ds.search_logs(body.server, keyword, minutes)}


@router.post("/ops/trace")
async def query_trace(
    body: ServerIn,
    user: User = Depends(require_permission("trace.view")),
):
    return await ds.fetch_trace(body.server)


# ---------- 服务器操控（二次确认 + 审计） ----------

@router.post("/ops/execute")
async def execute_command(
    body: ExecuteIn,
    request: Request,
    user: User = Depends(require_permission("ops.execute")),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else ""
    if any(w in body.command.lower() for w in DANGEROUS):
        await write_audit(db, user, "command.blocked", target=body.server,
                          detail={"command": body.command}, ip=ip, result="blocked")
        raise HTTPException(403, "危险命令已被安全策略拦截")

    r = get_redis()
    if not body.confirm_token:
        token = uuid.uuid4().hex
        await r.set(
            f"opsai:confirm:{token}",
            json.dumps({"username": user.username, "server": body.server,
                        "command": body.command}, ensure_ascii=False),
            ex=300,
        )
        await write_audit(db, user, "command.confirm_request", target=body.server,
                          detail={"command": body.command}, ip=ip)
        return {
            "need_confirm": True, "confirm_token": token, "expires_in": 300,
            "warning": "该操作将修改服务器状态，请在 5 分钟内携带 confirm_token 二次确认",
        }

    raw = await r.get(f"opsai:confirm:{body.confirm_token}")
    if not raw:
        raise HTTPException(400, "二次确认令牌无效或已过期")
    payload = json.loads(raw)
    if payload["username"] != user.username:
        raise HTTPException(403, "确认令牌与当前用户不匹配")
    if payload.get("command") != body.command or payload.get("server") != body.server:
        raise HTTPException(400, "确认令牌对应的命令与本次提交不一致，请重新确认")
    await r.delete(f"opsai:confirm:{body.confirm_token}")

    # 真实环境此步调用 Ansible playbook；本地返回模拟执行结果
    result = {
        "host": body.server, "command": body.command, "exit_code": 0,
        "stdout": "command executed (ansible 模拟执行)", "via": "ansible(模拟)",
    }
    await write_audit(db, user, "command.execute", target=body.server,
                      detail={"command": body.command, "result": result}, ip=ip)
    # 联动：控制台直接执行也会生成共享操作台账 + 工作日志，四端可见
    from app.routers_ops import create_operation_from_command

    code = await create_operation_from_command(
        db, user, body.server, body.command, result, ip=ip
    )
    return {"executed": True, "operation_code": code, **result}


# ---------- 工作流（状态机） ----------

@router.post("/workflows", status_code=201)
async def create_workflow(
    body: WorkflowIn,
    request: Request,
    user: User = Depends(require_permission("workflow.manage")),
    db: AsyncSession = Depends(get_db),
):
    exists = (await db.execute(select(WorkflowDef).where(WorkflowDef.name == body.name))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "同名工作流已存在")
    item = WorkflowDef(name=body.name, definition_json=json.dumps(body.definition, ensure_ascii=False))
    db.add(item)
    await db.commit()
    await write_audit(db, user, "workflow.create", target=body.name,
                      ip=request.client.host if request.client else "")
    return {"id": item.id, "name": item.name, "definition": body.definition}


@router.get("/workflows")
async def list_workflows(
    user: User = Depends(require_permission("workflow.manage")),
    db: AsyncSession = Depends(get_db),
):
    items = (await db.execute(select(WorkflowDef).order_by(WorkflowDef.id))).scalars().all()
    return [{"id": i.id, "name": i.name, "enabled": i.enabled,
             "definition": json.loads(i.definition_json)} for i in items]


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(
    workflow_id: int,
    body: WorkflowRunIn,
    request: Request,
    user: User = Depends(require_permission("workflow.manage")),
    db: AsyncSession = Depends(get_db),
):
    wf = await db.get(WorkflowDef, workflow_id)
    if wf is None:
        raise HTTPException(404, "工作流不存在")
    definition = json.loads(wf.definition_json)
    context = {"server": "0931", **body.context}

    run = WorkflowRun(def_id=workflow_id, status="running",
                      context_json=json.dumps(context, ensure_ascii=False))
    db.add(run)
    await db.flush()

    result = await WorkflowEngine(definition).run(context)
    run.status = result["status"]
    run.current_node = result["current_node"]
    run.context_json = json.dumps(result["context"], ensure_ascii=False)
    run.log_json = json.dumps(result["log"], ensure_ascii=False)
    run.finished_at = now_local()
    await db.commit()

    await write_audit(db, user, "workflow.run", target=wf.name,
                      detail={"run_id": run.id, "status": result["status"]},
                      ip=request.client.host if request.client else "",
                      result="success" if result["status"] == "succeeded" else "failed")
    return {"run_id": run.id, "workflow": wf.name, **result}


@router.get("/workflows/runs")
async def list_workflow_runs(
    user: User = Depends(require_permission("workflow.manage")),
    db: AsyncSession = Depends(get_db),
):
    runs = (await db.execute(select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(50))).scalars().all()
    return [{"id": r.id, "def_id": r.def_id, "status": r.status,
             "current_node": r.current_node, "log": json.loads(r.log_json),
             "started_at": fmt_dt(r.started_at)} for r in runs]


# ---------- 审计与报表 ----------

@router.get("/audit")
async def list_audit(
    limit: int = 100,
    user: User = Depends(require_permission("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars().all()
    return [{"id": a.id, "username": a.username, "action": a.action, "target": a.target,
             "detail": json.loads(a.detail_json), "ip": a.ip, "result": a.result,
             "created_at": fmt_dt(a.created_at)} for a in rows]


async def _report_data(db: AsyncSession) -> dict:
    users = await db.scalar(select(func.count()).select_from(User))
    convs = await db.scalar(select(func.count()).select_from(Conversation))
    msgs = await db.scalar(select(func.count()).select_from(Message))
    audits = await db.scalar(select(func.count()).select_from(AuditLog))
    commands = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "command.execute")
    )
    return {
        "generated_at": fmt_iso(),
        "system_health": 92.5,
        "total_users": users, "total_conversations": convs,
        "total_messages": msgs, "total_audit_events": audits,
        "commands_executed": commands,
        "incidents": [
            {"server": "0931", "level": "高", "problem": "磁盘空间耗尽导致写入失败", "status": "待处置"},
            {"server": "0932", "level": "低", "problem": "CPU 短时波动", "status": "已恢复"},
        ],
    }


@router.get("/reports/overview")
async def report_overview(
    user: User = Depends(require_permission("report.view")),
    db: AsyncSession = Depends(get_db),
):
    return await _report_data(db)


@router.get("/reports/export")
async def report_export(
    user: User = Depends(require_permission("report.export")),
    db: AsyncSession = Depends(get_db),
):
    data = await _report_data(db)
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="opsai_report.json"'},
    )
