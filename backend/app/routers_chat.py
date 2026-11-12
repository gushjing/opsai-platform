"""对话交互：自然语言排障、多轮上下文、记录管理与导出。"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_guard import detect, proposal_text, result_text, simulate
from app.agent import TOOL_REGISTRY, append_context, conversation_context, investigate
from app.db import get_db
from app.deps import get_redis, permission_codes, require_permission, write_audit
from app.domain_guard import OUT_OF_SCOPE_REPLY, classify
from app.models import Conversation, Message, User
from app.timeutil import fmt_dt, now_local

router = APIRouter(prefix="/api/chat", tags=["对话排障"])

INTENT_LABEL = {
    "diagnose": "根因诊断", "metrics": "指标查询", "logs": "日志检索",
    "trace": "链路定位", "suggestion": "处置建议", "server_info": "资产信息",
}


class AskIn(BaseModel):
    question: str
    conversation_id: int | None = None


class ActionIn(BaseModel):
    token: str
    decision: str  # confirm | cancel


def format_answer(c: dict) -> str:
    lines = [
        f"【问题】{c.get('problem', '未得出明确结论')}",
        f"【故障位置】{c.get('location', '未定位')}",
        "【判断依据】",
    ]
    lines += [f"  · {e}" for e in c.get("evidence", [])] or ["  · 暂无"]
    lines.append("【建议措施】")
    lines += [f"  {i}. {s}" for i, s in enumerate(c.get("suggestions", []), 1)] or ["  暂无"]
    lines.append(
        f"\n分析意图 {INTENT_LABEL.get(c.get('intent'), '排障分析')} · "
        f"置信度 {c.get('confidence', 0):.2f} · 分析模式 {c.get('mode')} · "
        f"数据范围 {c.get('data_scope')}"
    )
    if not c.get("server_explicit", True):
        lines.append("注：问题未指定服务器编号，按当前接入的默认数据源 0931 作答")
    return "\n".join(lines)


@router.post("/ask")
async def ask(
    body: AskIn,
    request: Request,
    user: User = Depends(require_permission("chat.ask")),
    db: AsyncSession = Depends(get_db),
):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    if body.conversation_id:
        conv = await db.get(Conversation, body.conversation_id)
        if conv is None or conv.user_id != user.id:
            raise HTTPException(404, "对话不存在或无权访问")
    else:
        conv = Conversation(user_id=user.id, title=question[:60], created_at=now_local())
        db.add(conv)
        await db.flush()

    db.add(Message(conversation_id=conv.id, role="user", content=question,
                   created_at=now_local()))
    await db.commit()
    await append_context(conv.id, "user", question)

    perms = permission_codes(user)
    ip = request.client.host if request.client else ""

    # 直接处置命令（如「帮我清理内存」）：先反馈执行计划并询问是否执行，
    # 不套用排障结论模板；确认后才真正动手并输出执行结果。
    action = detect(question, "ops.execute" in perms)
    if action:
        proposal = {k: v for k, v in action.items() if k != "question"}
        proposal["kind"] = "action_proposal"
        if action["allowed"]:
            token = uuid.uuid4().hex
            await get_redis().set(
                f"opsai:chataction:{token}",
                json.dumps({"username": user.username, "user_id": user.id,
                            "conversation_id": conv.id, "proposal": action},
                           ensure_ascii=False),
                ex=300,
            )
            proposal["token"] = token
            proposal["expires_in"] = 300
        answer = proposal_text(action)
        db.add(Message(
            conversation_id=conv.id, role="assistant", content=answer,
            structured_json=json.dumps(proposal, ensure_ascii=False),
            tool_trace_json="[]", created_at=now_local(),
        ))
        await db.commit()
        await append_context(conv.id, "assistant", answer)
        audit_name = ("chat.action_blocked" if action["dangerous"]
                      else "chat.action_propose" if action["allowed"]
                      else "chat.action_denied")
        await write_audit(
            db, user, audit_name, target=f"server:{action['server']}",
            detail={"conversation_id": conv.id, "question": question[:120],
                    "command": action["command"], "allowed": action["allowed"]},
            ip=ip, result="success" if action["allowed"] else "rejected",
        )
        return {
            "conversation_id": conv.id,
            "answer": answer,
            "structured": None,
            "action_proposal": proposal,
            "in_scope": True,
            "intent": "action_proposal" if action["allowed"] else "command_rejected",
            "permissions": sorted(perms),
        }

    history = await conversation_context(conv.id)
    guard = classify(question, history)

    # 领域外问题：固定拒答，不调用工具、不生成排障结论
    if not guard["in_scope"]:
        db.add(
            Message(
                conversation_id=conv.id, role="assistant",
                content=OUT_OF_SCOPE_REPLY, structured_json="", tool_trace_json="[]",
                created_at=now_local(),
            )
        )
        await db.commit()
        await append_context(conv.id, "assistant", OUT_OF_SCOPE_REPLY)
        await write_audit(
            db, user, "chat.out_of_scope", target="domain_guard",
            detail={"conversation_id": conv.id, "question": question[:120],
                    "reason": guard["reason"]},
            ip=ip, result="rejected",
        )
        return {
            "conversation_id": conv.id,
            "answer": OUT_OF_SCOPE_REPLY,
            "structured": None,
            "in_scope": False,
            "intent": "out_of_scope",
            "permissions": sorted(perms),
        }

    conclusion = await investigate(question, perms, conv.id, guard)
    answer = format_answer(conclusion)

    db.add(
        Message(
            conversation_id=conv.id,
            role="assistant",
            content=answer,
            structured_json=json.dumps(conclusion, ensure_ascii=False),
            tool_trace_json=json.dumps(conclusion.get("tool_trace", []), ensure_ascii=False),
            created_at=now_local(),
        )
    )
    await db.commit()
    await append_context(conv.id, "assistant", answer)

    await write_audit(
        db, user, "chat.ask", target=f"server:{conclusion.get('server')}",
        detail={"conversation_id": conv.id, "intent": guard["intent"],
                "question": question[:80],
                "tools": [t["tool"] for t in conclusion.get("tool_trace", [])]},
        ip=ip,
    )
    return {
        "conversation_id": conv.id,
        "answer": answer,
        "structured": conclusion,
        "in_scope": True,
        "intent": guard["intent"],
        "permissions": sorted(perms),
    }


@router.post("/action")
async def decide_action(
    body: ActionIn,
    request: Request,
    user: User = Depends(require_permission("chat.ask")),
    db: AsyncSession = Depends(get_db),
):
    """确认 / 取消 Agent 的处置提案。

    命令内容只从服务端令牌里读，前端不传 command，杜绝「拿确认令牌换一条命令」；
    令牌一次性消费，并严格绑定账号与会话。
    """
    token = body.token.strip()
    decision = body.decision.strip().lower()
    if decision not in ("confirm", "cancel"):
        raise HTTPException(400, "decision 只能是 confirm 或 cancel")
    key = f"opsai:chataction:{token}"
    r = get_redis()
    raw = await r.get(key)
    if not raw:
        raise HTTPException(400, "该确认已超时或已处理，请重新下达处置命令")
    payload = json.loads(raw)
    if payload.get("username") != user.username or payload.get("user_id") != user.id:
        raise HTTPException(403, "确认请求与当前账号不匹配")
    conv_id = payload.get("conversation_id")
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(403, "确认请求与当前会话不匹配")
    await r.delete(key)  # 一次性，重复点击不会二次执行

    proposal = payload["proposal"]
    ip = request.client.host if request.client else ""
    operation_code = ""

    if decision == "cancel":
        answer = (f"已取消「{proposal['summary']}」，未对服务器做任何改动。"
                  "如需处置，可重新下达命令。")
        await write_audit(
            db, user, "chat.action_cancel", target=f"server:{proposal['server']}",
            detail={"conversation_id": conv_id, "command": proposal["command"]},
            ip=ip, result="cancelled",
        )
        outcome = {"kind": "action_result", "decision": "cancel"}
    else:
        result = simulate(proposal)
        from app.routers_ops import create_operation_from_command

        operation_code = await create_operation_from_command(
            db, user, proposal["server"], proposal["command"], result,
            ip=ip, origin="对话 Agent 确认后",
        )
        answer = result_text(result, operation_code)
        await write_audit(
            db, user, "chat.action_execute", target=f"server:{proposal['server']}",
            detail={"conversation_id": conv_id, "command": proposal["command"],
                    "exit_code": result["exit_code"], "operation_code": operation_code},
            ip=ip,
        )
        outcome = {
            "kind": "action_result", "decision": "confirm",
            "result": result, "operation_code": operation_code,
        }

    db.add(Message(
        conversation_id=conv_id, role="assistant", content=answer,
        structured_json=json.dumps(outcome, ensure_ascii=False),
        tool_trace_json="[]", created_at=now_local(),
    ))
    await db.commit()
    await append_context(conv_id, "assistant", answer)
    return {
        "conversation_id": conv_id,
        "answer": answer,
        "decision": decision,
        "action_result": outcome,
        "operation_code": operation_code,
    }


@router.get("/conversations")
async def list_conversations(
    user: User = Depends(require_permission("chat.view")),
    db: AsyncSession = Depends(get_db),
):
    # 四端对话记录严格隔离：任何角色都只能看到自己账号下的对话，
    # 包括管理员（管理员查看提问人身份走 /api/admin/inquiries，不在此暴露聊天内容）。
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(100)
    )
    convs = (await db.execute(stmt)).scalars().all()
    result = []
    for c in convs:
        count = await db.scalar(
            select(func.count()).select_from(Message).where(Message.conversation_id == c.id)
        )
        result.append({"id": c.id, "title": c.title, "user_id": c.user_id,
                       "message_count": count, "created_at": fmt_dt(c.created_at)})
    return result


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    user: User = Depends(require_permission("chat.view")),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "对话不存在")
    if conv.user_id != user.id:
        raise HTTPException(403, "对话记录按账号隔离，无权查看他人对话")
    messages = (
        await db.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        )
    ).scalars().all()
    return {
        "id": conv.id,
        "title": conv.title,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "structured": json.loads(m.structured_json) if m.structured_json else None,
                "tool_trace": json.loads(m.tool_trace_json) if m.tool_trace_json else [],
                "created_at": fmt_dt(m.created_at),
            }
            for m in messages
        ],
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    request: Request,
    user: User = Depends(require_permission("chat.manage")),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "对话不存在")
    if conv.user_id != user.id:
        raise HTTPException(403, "对话记录按账号隔离，无权删除他人对话")
    await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await db.commit()
    await write_audit(
        db, user, "chat.delete", target=f"conversation:{conversation_id}",
        ip=request.client.host if request.client else "",
    )
    return {"deleted": conversation_id}


@router.delete("/history")
async def clear_my_history(
    request: Request,
    user: User = Depends(require_permission("chat.view")),
    db: AsyncSession = Depends(get_db),
):
    """清空当前账号的全部对话记录（侧栏「清空历史」按钮的真实交互）。"""
    ids = (
        await db.execute(
            select(Conversation.id).where(Conversation.user_id == user.id)
        )
    ).scalars().all()
    if ids:
        await db.execute(delete(Message).where(Message.conversation_id.in_(ids)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(ids)))
        await db.commit()
    if ids:
        try:
            await get_redis().delete(*[f"opsai:conv:{cid}" for cid in ids])
        except Exception:
            pass
    await write_audit(
        db, user, "chat.history_clear", target=f"user:{user.username}",
        detail={"deleted_conversations": len(ids)},
        ip=request.client.host if request.client else "",
    )
    return {"deleted_conversations": len(ids)}


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: int,
    user: User = Depends(require_permission("chat.manage")),
    db: AsyncSession = Depends(get_db),
):
    data = await get_conversation(conversation_id, user, db)
    return {"filename": f"conversation_{conversation_id}.json", "content": data}


@router.get("/tools")
async def list_tools(user: User = Depends(require_permission("chat.view"))):
    return [
        {"name": name, "description": t.description.split("\n")[0]}
        for name, t in TOOL_REGISTRY.items()
    ]
