"""提问人身份查看 + 管理员发起的一对一互动私聊。

管理员可在「提问人」面板看到每个提问账号的真实身份（账号、姓名、角色、
最近提问、来源 IP），并直接发起私聊；双方都能收发消息，形成真实双向互动。
非会话参与者无法读取消息，非管理员无法发起会话。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.deps import require_permission, role_codes, write_audit
from app.models import AuditLog, Conversation, Message, PrivateMessage, PrivateThread, User
from app.timeutil import fmt_dt, now_local

router = APIRouter(prefix="/api", tags=["提问人与私聊"])

ROLE_LABEL = {
    "ADMIN": "管理员", "ENGINEER": "工程师", "USER": "普通用户", "LEADER": "领导",
}


class ThreadIn(BaseModel):
    target_user_id: int
    subject: str = ""


class MessageIn(BaseModel):
    content: str


def _identity(u: User) -> dict:
    codes = role_codes(u)
    primary = codes[0] if codes else ""
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name or u.username,
        "roles": codes,
        "role_label": ROLE_LABEL.get(primary, primary or "未分配"),
        "is_active": bool(u.is_active),
    }


async def _my_threads(db: AsyncSession, user: User) -> list[PrivateThread]:
    return (
        await db.execute(
            select(PrivateThread)
            .where(or_(PrivateThread.admin_id == user.id, PrivateThread.target_id == user.id))
            .order_by(PrivateThread.updated_at.desc())
        )
    ).scalars().all()


async def _unread(db: AsyncSession, thread_id: int, me: int) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(PrivateMessage)
        .where(
            PrivateMessage.thread_id == thread_id,
            PrivateMessage.sender_id != me,
            PrivateMessage.is_read.is_(False),
        )
    ) or 0


async def _thread_payload(db: AsyncSession, t: PrivateThread, me: User, other: User | None) -> dict:
    return {
        "id": t.id,
        "subject": t.subject,
        "last_message": t.last_message,
        "created_at": fmt_dt(t.created_at),
        "updated_at": fmt_dt(t.updated_at),
        "unread": await _unread(db, t.id, me.id),
        "is_initiator": t.admin_id == me.id,
        "other": _identity(other) if other is not None else None,
    }


async def _thread_for(db: AsyncSession, thread_id: int, user: User) -> PrivateThread:
    t = await db.get(PrivateThread, thread_id)
    if t is None:
        raise HTTPException(404, "私聊会话不存在")
    if user.id not in (t.admin_id, t.target_id):
        raise HTTPException(403, "仅会话双方可查看或发送私聊消息")
    return t


def _msg_dict(m: PrivateMessage, me: User) -> dict:
    return {
        "id": m.id,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "content": m.content,
        "mine": m.sender_id == me.id,
        "is_read": bool(m.is_read),
        "created_at": fmt_dt(m.created_at),
    }


# ---------- 管理员：查看提问人身份 ----------

@router.get("/admin/inquiries")
async def list_inquiries(
    user: User = Depends(require_permission("inquiry.view")),
    db: AsyncSession = Depends(get_db),
):
    """只暴露身份与提问摘要，不返回他人对话正文（四端聊天记录仍严格隔离）。"""
    users = (
        await db.execute(
            select(User).options(selectinload(User.roles)).order_by(User.id)
        )
    ).scalars().all()
    threads = (
        await db.execute(
            select(PrivateThread).where(PrivateThread.admin_id == user.id)
        )
    ).scalars().all()
    thread_map = {t.target_id: t.id for t in threads}

    result = []
    for u in users:
        conv_count = await db.scalar(
            select(func.count()).select_from(Conversation)
            .where(Conversation.user_id == u.id)
        ) or 0
        question_count = await db.scalar(
            select(func.count()).select_from(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == u.id, Message.role == "user")
        ) or 0
        last_q = (
            await db.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.user_id == u.id, Message.role == "user")
                .order_by(Message.id.desc())
                .limit(1)
            )
        ).scalars().first()
        last_ask = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.user_id == u.id, AuditLog.action == "chat.ask")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        ).scalars().first()
        result.append({
            **_identity(u),
            "is_self": u.id == user.id,
            "conversation_count": conv_count,
            "question_count": question_count,
            "last_question": (last_q.content[:80] if last_q else ""),
            "last_question_at": fmt_dt(last_q.created_at) if last_q else "",
            "last_ip": last_ask.ip if last_ask else "",
            "thread_id": thread_map.get(u.id),
        })
    return result


# ---------- 会话（仅管理员可发起） ----------

@router.post("/private/threads", status_code=201)
async def open_thread(
    body: ThreadIn,
    request: Request,
    user: User = Depends(require_permission("inquiry.view")),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, body.target_user_id)
    if target is None:
        raise HTTPException(404, "目标用户不存在")
    if target.id == user.id:
        raise HTTPException(400, "不能与自己发起私聊")
    ip = request.client.host if request.client else ""
    existing = (
        await db.execute(
            select(PrivateThread).where(
                PrivateThread.admin_id == user.id,
                PrivateThread.target_id == target.id,
            )
        )
    ).scalars().first()
    if existing:
        return await _thread_payload(db, existing, user, target)

    greeting = (
        f"管理员 {user.display_name or user.username} 已与你建立一对一沟通，"
        "如有排障问题请直接在此回复。"
    )
    t = PrivateThread(
        admin_id=user.id, target_id=target.id,
        subject=body.subject.strip() or "排障问题沟通",
        last_message=greeting[:100], created_at=now_local(), updated_at=now_local(),
    )
    db.add(t)
    await db.flush()
    db.add(PrivateMessage(
        thread_id=t.id, sender_id=user.id,
        sender_name=user.display_name or user.username,
        content=greeting, is_read=False, created_at=now_local(),
    ))
    await db.commit()
    await write_audit(
        db, user, "private.thread.open", target=f"user:{target.username}",
        detail={"thread_id": t.id, "target_user_id": target.id}, ip=ip,
    )
    return await _thread_payload(db, t, user, target)


@router.get("/private/threads")
async def list_threads(
    user: User = Depends(require_permission("private.chat")),
    db: AsyncSession = Depends(get_db),
):
    result = []
    for t in await _my_threads(db, user):
        other_id = t.target_id if t.admin_id == user.id else t.admin_id
        other = await db.get(User, other_id)
        result.append(await _thread_payload(db, t, user, other))
    return result


@router.get("/private/unread")
async def unread_summary(
    user: User = Depends(require_permission("private.chat")),
    db: AsyncSession = Depends(get_db),
):
    rows = []
    total = 0
    for t in await _my_threads(db, user):
        n = await _unread(db, t.id, user.id)
        total += n
        rows.append({"id": t.id, "unread": n})
    return {"unread": total, "threads": rows}


@router.get("/private/threads/{thread_id}/messages")
async def list_messages(
    thread_id: int,
    user: User = Depends(require_permission("private.chat")),
    db: AsyncSession = Depends(get_db),
):
    t = await _thread_for(db, thread_id, user)
    msgs = (
        await db.execute(
            select(PrivateMessage)
            .where(PrivateMessage.thread_id == thread_id)
            .order_by(PrivateMessage.id)
        )
    ).scalars().all()
    dirty = False
    for m in msgs:
        if m.sender_id != user.id and not m.is_read:
            m.is_read = True
            dirty = True
    if dirty:
        await db.commit()
    other_id = t.target_id if t.admin_id == user.id else t.admin_id
    other = await db.get(User, other_id)
    return {
        "thread": await _thread_payload(db, t, user, other),
        "messages": [_msg_dict(m, user) for m in msgs],
    }


@router.post("/private/threads/{thread_id}/messages", status_code=201)
async def send_message(
    thread_id: int,
    body: MessageIn,
    request: Request,
    user: User = Depends(require_permission("private.chat")),
    db: AsyncSession = Depends(get_db),
):
    t = await _thread_for(db, thread_id, user)
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "消息内容不能为空")
    m = PrivateMessage(
        thread_id=t.id, sender_id=user.id,
        sender_name=user.display_name or user.username,
        content=content[:2000], is_read=False, created_at=now_local(),
    )
    db.add(m)
    t.last_message = f"{m.sender_name}：{content[:70]}"
    t.updated_at = now_local()
    await db.commit()
    await write_audit(
        db, user, "private.message.send", target=f"thread:{t.id}",
        detail={"thread_id": t.id, "length": len(content)},
        ip=request.client.host if request.client else "",
    )
    return _msg_dict(m, user)
