import json

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import get_db
from app.models import AuditLog, Role, User
from app.security import decode_access_token
from app.timeutil import now_local

bearer = HTTPBearer(auto_error=False)

# Redis 5.0 不支持 HELLO，统一走 RESP2
redis_client = aioredis.from_url(
    settings.redis_url, protocol=2, decode_responses=True
)


def get_redis():
    return redis_client


def permission_codes(user: User) -> set[str]:
    return {p.code for role in user.roles for p in role.permissions}


def role_codes(user: User) -> list[str]:
    return [role.code for role in user.roles]


async def current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = decode_access_token(cred.credentials)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    result = await db.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.username == payload.get("sub"))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不可用")
    request.state.user = user
    return user


def require_permission(*codes: str):
    """接口级权限注解：Depends(require_permission("ops.execute"))。"""

    async def checker(user: User = Depends(current_user)) -> User:
        owned = permission_codes(user)
        missing = [c for c in codes if c not in owned]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少权限: {', '.join(missing)}",
            )
        return user

    return checker


async def write_audit(
    db: AsyncSession,
    user: User,
    action: str,
    target: str = "",
    detail: dict | None = None,
    ip: str = "",
    result: str = "success",
) -> None:
    """所有敏感操作统一审计留痕（PRD 2.2.4）。"""
    db.add(
        AuditLog(
            user_id=user.id,
            username=user.username,
            action=action,
            target=target,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
            ip=ip,
            result=result,
            created_at=now_local(),
        )
    )
    await db.commit()
