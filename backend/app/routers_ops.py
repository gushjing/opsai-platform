"""四端协同处置：操作台账、工作日志时间线、状态流转、接单执行、全局动态。

设计要点：任何一端对服务器发起的「处理」都会生成一条共享操作记录；
之后每一次工作日志更新、接单、命令执行，都会同时写入
① 操作台账（last_action / 状态 / 更新时间）② 工作日志时间线 ③ 系统审计日志，
并保证其他三个角色端可见。
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import (
    get_redis,
    permission_codes,
    require_permission,
    role_codes,
    write_audit,
)
from app.models import AuditLog, OperationRecord, WorkLog
from app.timeutil import fmt_dt, now_local

router = APIRouter(prefix="/api", tags=["协同处置"])

DANGEROUS = ["rm -rf /", "mkfs", ":(){", "shutdown -h", "dd if=/dev/zero"]
VALID_STATUS = {"pending": "待处理", "processing": "处理中", "resolved": "已解决", "closed": "已关闭"}
STATUS_ORDER = ["pending", "processing", "resolved", "closed"]

ACTION_LABEL = {
    "operation.create": "发起工单", "operation.merge": "报修合并",
    "operation.touch": "工单更新", "operation.status_change": "状态流转",
    "operation.action_request": "提交处理请求", "operation.auto_resolved": "工单自动办结",
    "worklog.append": "更新工作日志", "command.execute": "执行处置",
    "command.confirm_request": "请求执行确认", "command.blocked": "安全拦截",
    "chat.ask": "发起排障", "chat.out_of_scope": "领域外提问",
    "chat.history_clear": "清空对话历史", "chat.delete": "删除对话记录",
    "chat.action_propose": "等待处置确认", "chat.action_execute": "执行处置",
    "chat.action_cancel": "取消处置", "chat.action_denied": "无执行权限",
    "chat.action_blocked": "高危命令拦截",
    "auth.demo_switch": "切换身份", "workflow.run": "运行工作流",
    "workflow.create": "新建工作流", "datasource.create": "登记数据源",
    "datasource.delete": "删除数据源", "report.export": "导出报表",
    "private.thread.open": "发起私聊", "private.message.send": "私聊消息",
    "private.message": "私聊消息",
}


class OperationIn(BaseModel):
    title: str = ""
    server: str = "0931"
    issue: str = ""
    source_conversation_id: int = 0


class WorkLogIn(BaseModel):
    content: str
    request_action: str = ""


class HandleIn(BaseModel):
    status: str
    note: str = ""


class ExecuteIn(BaseModel):
    command: str
    confirm_token: str | None = None


def _now() -> datetime:
    return now_local()


def _ts(value) -> float:
    """用于同秒内的稳定排序（不影响对外展示）。"""
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _code() -> str:
    return f"OP-{now_local():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}"


def _role(user) -> str:
    codes = role_codes(user)
    return codes[0] if codes else ""


def _display(user) -> str:
    """报修人展示名：显示名（账号），保证管理员能看清是谁提的问题。"""
    name = (getattr(user, "display_name", "") or user.username)
    return f"{name}（{user.username}）" if name != user.username else user.username


def _norm(text: str) -> str:
    """问题指纹归一化：忽略大小写、空白与标点，保留汉字与字母数字。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def _issue_key(server: str, title: str) -> str:
    raw = f"{server}|{_norm(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _reporter_names(rec: OperationRecord) -> list[str]:
    raw = (rec.reporters or "").strip()
    return [x for x in raw.split("、") if x] if raw else [rec.requester]


def _reporter_id_set(rec: OperationRecord) -> set[str]:
    """reporter_ids 存成 ,1,2, 形式，便于用 LIKE '%,2,%' 做精确包含查询。"""
    raw = (rec.reporter_ids or "").strip()
    return {x for x in raw.split(",") if x} if raw else {str(rec.requester_id)}


def _involved(stmt, user):
    """普通用户可见范围：自己发起的，或被合并进去的工单。"""
    return stmt.where(
        or_(
            OperationRecord.requester_id == user.id,
            OperationRecord.reporter_ids.like(f"%,{user.id},%"),
        )
    )


def _record_dict(rec: OperationRecord) -> dict:
    names = _reporter_names(rec)
    return {
        "id": rec.id,
        "code": rec.code,
        "title": rec.title,
        "server": rec.server,
        "issue": rec.issue,
        "source_conversation_id": rec.source_conversation_id,
        "requester": rec.requester,
        "requester_role": rec.requester_role,
        "reporters": "、".join(names),
        "reporter_names": names,
        "reporter_count": len(names),
        "merged": len(names) > 1,
        "status": rec.status,
        "status_label": VALID_STATUS.get(rec.status, rec.status),
        "assignee": rec.assignee,
        "last_action": rec.last_action,
        "pending_action": rec.pending_action,
        "created_at": fmt_dt(rec.created_at),
        "updated_at": fmt_dt(rec.updated_at),
    }


def _log_dict(log: WorkLog) -> dict:
    return {
        "id": log.id,
        "username": log.username,
        "role": log.role,
        "action": log.action,
        "content": log.content,
        "created_at": fmt_dt(log.created_at),
    }


async def _visible(db: AsyncSession, rec_id: int, user) -> OperationRecord:
    rec = await db.get(OperationRecord, rec_id)
    if rec is None:
        raise HTTPException(404, "操作记录不存在")
    perms = permission_codes(user)
    # 合并报修后，所有报修人都算「自己人」，即使不是最初的发起人也能查看工单
    own = rec.requester_id == user.id or str(user.id) in _reporter_id_set(rec)
    if not own and not ({"operation.handle", "audit.view"} & perms):
        raise HTTPException(403, "无权查看该操作记录")
    return rec


async def _append_log(
    db: AsyncSession,
    rec: OperationRecord,
    user,
    action: str,
    content: str,
    ip: str = "",
    audit_action: str = "worklog.append",
) -> None:
    """一次更新，三处留痕：台账摘要 + 工作日志时间线 + 系统审计日志。"""
    code = rec.code
    db.add(
        WorkLog(
            record_id=rec.id,
            user_id=user.id,
            username=user.username,
            role=_role(user),
            action=action,
            content=content,
            created_at=_now(),
        )
    )
    rec.last_action = f"{user.username} · {action}：{content[:70]}"
    rec.updated_at = _now()
    await db.flush()
    await write_audit(
        db, user, audit_action, target=f"operation:{code}",
        detail={"record_id": rec.id, "code": code, "action": action, "content": content},
        ip=ip,
    )
    await db.refresh(rec)


async def _detail(db: AsyncSession, rec_id: int, user) -> dict:
    rec = await _visible(db, rec_id, user)
    logs = (
        await db.execute(
            select(WorkLog).where(WorkLog.record_id == rec_id).order_by(WorkLog.id)
        )
    ).scalars().all()
    audits = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.target == f"operation:{rec.code}")
            .order_by(AuditLog.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return {
        "record": _record_dict(rec),
        "worklogs": [_log_dict(x) for x in logs],
        "related_audits": [
            {
                "id": a.id, "username": a.username, "action": a.action,
                "result": a.result, "created_at": fmt_dt(a.created_at),
            }
            for a in audits
        ],
    }


async def create_operation_from_command(
    db: AsyncSession, user, server: str, command: str, result: dict,
    ip: str = "", origin: str = "运维控制台"
) -> str:
    """执行命令后自动补一条操作台账，保证四端可见（控制台 / 对话 Agent 共用）。"""
    code = _code()
    rec = OperationRecord(
        code=code,
        title=f"{server} 服务器命令执行",
        server=server,
        issue=f"{origin}执行运维命令：{command}",
        requester_id=user.id,
        requester=user.username,
        requester_role=_role(user),
        reporters=_display(user),
        reporter_ids=f",{user.id},",
        issue_key=_issue_key(server, f"{server} 服务器命令执行"),
        status="resolved",
        assignee=user.username,
        last_action=f"{user.username} 执行命令 {command}",
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(rec)
    await db.flush()
    content = (
        f"通过运维控制台执行命令 `{command}`；exit_code={result.get('exit_code')}，"
        f"通道={result.get('via')}。结果：{str(result.get('stdout', ''))[:300]}"
    )
    await _append_log(db, rec, user, "命令执行", content, ip=ip,
                      audit_action="command.execute")
    await write_audit(
        db, user, "operation.create", target=f"operation:{code}",
        detail={"origin": "console", "code": code, "server": server, "command": command},
        ip=ip,
    )
    return code


# ---------- 操作台账 ----------

@router.post("/operations", status_code=201)
async def create_operation(
    body: OperationIn,
    request: Request,
    user=Depends(require_permission("operation.create")),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else ""
    server = (body.server or "0931").strip()
    title = body.title.strip() or f"{server} 服务器处理申请"
    issue = body.issue.strip() or "由对话排障结论发起的服务器处理申请"
    key = _issue_key(server, title)

    # 同一服务器 + 同一问题（指纹相同或标题高度相似）且工单未办结 → 合并报修，
    # 不重复建单；所有报修人的名字都会留在这张工单上。
    candidates = (
        await db.execute(
            select(OperationRecord)
            .where(OperationRecord.server == server,
                   OperationRecord.status.in_(("pending", "processing")))
            .order_by(OperationRecord.id)
        )
    ).scalars().all()
    normalized = _norm(title)
    target = None
    for c in candidates:
        if c.issue_key == key or (
            normalized and _norm(c.title)
            and SequenceMatcher(None, normalized, _norm(c.title)).ratio() >= 0.86
        ):
            target = c
            break

    if target is not None:
        names = _reporter_names(target)
        ids = _reporter_id_set(target)
        label = _display(user)
        if str(user.id) not in ids:
            ids.add(str(user.id))
            names.append(label)
        target.reporters = "、".join(names)
        target.reporter_ids = "," + ",".join(sorted(ids, key=lambda x: int(x) if x.isdigit() else 0)) + ","
        target.issue_key = key
        await db.flush()
        await _append_log(
            db, target, user, "报修合并",
            f"{label} 也反馈了同一问题，已合并到本工单，不再重复建单；"
            f"当前报修人共 {len(names)} 人：{target.reporters}",
            ip=ip, audit_action="operation.merge",
        )
        await write_audit(
            db, user, "operation.merge", target=f"operation:{target.code}",
            detail={"record_id": target.id, "code": target.code, "server": server,
                    "reporters": target.reporters, "reporter_count": len(names),
                    "source_conversation_id": body.source_conversation_id},
            ip=ip,
        )
        return {
            "merged": True,
            "message": f"相同问题已合并到工单 {target.code}，报修人：{target.reporters}",
            **await _detail(db, target.id, user),
        }

    code = _code()
    rec = OperationRecord(
        code=code,
        title=title,
        server=server,
        issue=issue,
        source_conversation_id=body.source_conversation_id,
        requester_id=user.id,
        requester=user.username,
        requester_role=_role(user),
        reporters=_display(user),
        reporter_ids=f",{user.id},",
        issue_key=key,
        status="pending",
        last_action=f"{user.username} 发起处理申请",
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(rec)
    await db.flush()
    rid = rec.id
    await _append_log(
        db, rec, user, "发起申请",
        f"针对 {server} 发起服务器处理申请：{title}。问题：{issue}",
        ip=ip, audit_action="worklog.append",
    )
    await write_audit(
        db, user, "operation.create", target=f"operation:{code}",
        detail={"record_id": rid, "code": code, "server": server,
                "source_conversation_id": body.source_conversation_id},
        ip=ip,
    )
    return {"merged": False, **await _detail(db, rid, user)}


@router.get("/operations/summary")
async def operation_summary(
    user=Depends(require_permission("operation.view")),
    db: AsyncSession = Depends(get_db),
):
    perms = permission_codes(user)
    stmt = select(OperationRecord.status, func.count()).group_by(OperationRecord.status)
    if not ({"operation.handle", "audit.view"} & perms):
        stmt = _involved(stmt, user)
    rows = (await db.execute(stmt)).all()
    counts = {k: 0 for k in STATUS_ORDER}
    for status, n in rows:
        counts[status] = n
    return {"counts": counts, "pending": counts["pending"],
            "processing": counts["processing"], "total": sum(counts.values())}


@router.get("/operations")
async def list_operations(
    status: str = "",
    limit: int = 100,
    user=Depends(require_permission("operation.view")),
    db: AsyncSession = Depends(get_db),
):
    perms = permission_codes(user)
    stmt = select(OperationRecord).order_by(OperationRecord.updated_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(OperationRecord.status == status)
    if not ({"operation.handle", "audit.view"} & perms):
        stmt = _involved(stmt, user)
    recs = (await db.execute(stmt)).scalars().all()
    result = []
    for rec in recs:
        latest = (
            await db.execute(
                select(WorkLog).where(WorkLog.record_id == rec.id).order_by(WorkLog.id.desc()).limit(1)
            )
        ).scalars().first()
        result.append({
            **_record_dict(rec),
            "worklog_count": await db.scalar(
                select(func.count()).select_from(WorkLog).where(WorkLog.record_id == rec.id)
            ),
            "latest_log": _log_dict(latest) if latest else None,
        })
    return result


@router.get("/operations/{rec_id}")
async def get_operation(
    rec_id: int,
    user=Depends(require_permission("operation.view")),
    db: AsyncSession = Depends(get_db),
):
    return await _detail(db, rec_id, user)


@router.post("/operations/{rec_id}/worklogs", status_code=201)
async def append_worklog(
    rec_id: int,
    body: WorkLogIn,
    request: Request,
    user=Depends(require_permission("worklog.write")),
    db: AsyncSession = Depends(get_db),
):
    rec = await _visible(db, rec_id, user)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "工作日志内容不能为空")
    ip = request.client.host if request.client else ""
    if body.request_action.strip():
        rec.pending_action = body.request_action.strip()
        action = "提交处理请求"
        content = f"{content}；申请动作：{rec.pending_action}"
        audit_action = "operation.action_request"
    else:
        action = "工作日志更新"
        audit_action = "worklog.append"
    await _append_log(db, rec, user, action, content, ip=ip, audit_action=audit_action)
    await write_audit(
        db, user, "operation.touch", target=f"operation:{rec.code}",
        detail={"record_id": rec.id, "code": rec.code, "last_action": rec.last_action},
        ip=ip,
    )
    return await _detail(db, rec_id, user)


@router.post("/operations/{rec_id}/handle")
async def handle_operation(
    rec_id: int,
    body: HandleIn,
    request: Request,
    user=Depends(require_permission("operation.handle")),
    db: AsyncSession = Depends(get_db),
):
    rec = await _visible(db, rec_id, user)
    if body.status not in VALID_STATUS:
        raise HTTPException(400, f"状态不合法，可选：{', '.join(VALID_STATUS)}")
    ip = request.client.host if request.client else ""
    old = rec.status
    rec.status = body.status
    if body.status == "processing":
        rec.assignee = user.username
    if body.status in ("resolved", "closed"):
        rec.pending_action = ""
    label = VALID_STATUS[body.status]
    note = body.note.strip()
    content = f"状态由「{VALID_STATUS.get(old, old)}」变更为「{label}」"
    if body.status == "processing":
        content += f"，由 {user.username} 接单处理"
    if note:
        content += f"。备注：{note}"
    await _append_log(db, rec, user, "状态流转", content, ip=ip,
                      audit_action="operation.status_change")
    return await _detail(db, rec_id, user)


@router.post("/operations/{rec_id}/execute")
async def execute_on_operation(
    rec_id: int,
    body: ExecuteIn,
    request: Request,
    user=Depends(require_permission("operation.handle", "ops.execute")),
    db: AsyncSession = Depends(get_db),
):
    rec = await _visible(db, rec_id, user)
    ip = request.client.host if request.client else ""
    command = body.command.strip()
    if not command:
        raise HTTPException(400, "命令不能为空")
    if any(w in command.lower() for w in DANGEROUS):
        await _append_log(db, rec, user, "安全拦截",
                          f"危险命令 `{command}` 被安全策略拦截，未执行", ip=ip,
                          audit_action="command.blocked")
        raise HTTPException(403, "危险命令已被安全策略拦截")

    r = get_redis()
    if not body.confirm_token:
        token = uuid.uuid4().hex
        await r.set(
            f"opsai:opconfirm:{token}",
            json.dumps({"username": user.username, "record_id": rec.id,
                        "command": command}, ensure_ascii=False),
            ex=300,
        )
        await write_audit(
            db, user, "command.confirm_request", target=f"operation:{rec.code}",
            detail={"record_id": rec.id, "code": rec.code, "command": command}, ip=ip,
        )
        return {"need_confirm": True, "confirm_token": token, "expires_in": 300,
                "record_code": rec.code,
                "warning": "该操作将修改服务器状态，请在 5 分钟内携带 confirm_token 二次确认"}

    raw = await r.get(f"opsai:opconfirm:{body.confirm_token}")
    if not raw:
        raise HTTPException(400, "二次确认令牌无效或已过期")
    payload = json.loads(raw)
    if payload["username"] != user.username or payload["record_id"] != rec.id:
        raise HTTPException(403, "确认令牌与当前用户或工单不匹配")
    if payload.get("command") != command:
        raise HTTPException(400, "确认令牌对应的命令与本次提交不一致，请重新确认")
    await r.delete(f"opsai:opconfirm:{body.confirm_token}")

    result = {
        "host": rec.server, "command": command, "exit_code": 0,
        "stdout": "command executed (ansible 模拟执行)", "via": "ansible(模拟)",
    }
    rec.status = "resolved"
    rec.assignee = rec.assignee or user.username
    rec.pending_action = ""
    await _append_log(
        db, rec, user, "执行处置",
        f"在 {rec.server} 执行命令 `{command}`；exit_code=0，通道={result['via']}。"
        f"工单自动置为已解决。",
        ip=ip, audit_action="command.execute",
    )
    await write_audit(
        db, user, "operation.auto_resolved", target=f"operation:{rec.code}",
        detail={"record_id": rec.id, "code": rec.code, "command": command,
                "result": result}, ip=ip,
    )
    return {"executed": True, **result, **await _detail(db, rec_id, user)}


# ---------- 全局操作动态（四端共享） ----------

@router.get("/activity")
async def activity_stream(
    limit: int = 60,
    user=Depends(require_permission("activity.view")),
    db: AsyncSession = Depends(get_db),
):
    """合并系统审计日志与工作日志时间线，形成跨角色可见的操作流。

    record_id 只在当前账号确实有权打开该工单时返回，前端据此决定是否渲染
    可点击的工单链接，避免出现「点了没反应」的假按钮。
    """
    # 兜底访问日志（request.write）与登录记录数量极大但没有业务含义，
    # 会把真正的四端动作刷没，这里在动态流里剔除（审计表本身仍完整保留）。
    noise = {"request.write", "auth.login"}
    audits = (
        await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit * 4))
    ).scalars().all()
    audits = [a for a in audits if a.action not in noise][:limit]
    logs = (
        await db.execute(select(WorkLog).order_by(WorkLog.id.desc()).limit(limit))
    ).scalars().all()
    perms = permission_codes(user)
    can_see_all = bool({"operation.handle", "audit.view"} & perms)

    all_recs = (await db.execute(select(OperationRecord))).scalars().all()
    id_to_code = {r.id: r.code for r in all_recs}
    code_to_rec = {r.code: r for r in all_recs}

    def linkable(rec: OperationRecord | None):
        if rec is None:
            return "", None
        if (not can_see_all and rec.requester_id != user.id
                and str(user.id) not in _reporter_id_set(rec)):
            return rec.code, None
        return rec.code, rec.id

    items = []
    for a in audits:
        code = a.target.split(":", 1)[1] if a.target.startswith("operation:") else ""
        rec_code, rec_id = linkable(code_to_rec.get(code))
        label = ACTION_LABEL.get(a.action, a.action)
        try:
            detail = json.loads(a.detail_json or "{}")
        except (ValueError, TypeError):
            detail = {}
        brief = (detail.get("content") or detail.get("question")
                 or detail.get("command") or "")
        if not brief and a.target and not a.target.startswith("operation:"):
            brief = a.target
        if brief.startswith("server:"):
            brief = "服务器 " + brief.split(":", 1)[1]
        elif brief.startswith("thread:"):
            brief = "会话 " + brief.split(":", 1)[1]
        items.append({
            "key": f"a-{a.id}", "kind": "audit", "username": a.username,
            "action": label, "raw_action": a.action, "target": a.target,
            "content": f"{label}{'：' + str(brief)[:90] if brief else ''}",
            "result": a.result, "record_code": rec_code, "record_id": rec_id,
            "created_at": fmt_dt(a.created_at), "_ts": _ts(a.created_at),
        })
    for w in logs:
        rec = code_to_rec.get(id_to_code.get(w.record_id, ""))
        rec_code, rec_id = linkable(rec)
        items.append({
            "key": f"w-{w.id}", "kind": "worklog", "username": w.username,
            "action": w.action, "target": rec_code,
            "content": w.content, "result": "success",
            "record_code": rec_code, "record_id": rec_id,
            "created_at": fmt_dt(w.created_at), "_ts": _ts(w.created_at),
        })
    items.sort(key=lambda x: (x["created_at"], x["_ts"]), reverse=True)
    for x in items:
        x.pop("_ts", None)
    return items[:limit]
