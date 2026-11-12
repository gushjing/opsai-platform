"""认证与 RBAC 权限管理接口。"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import get_db
from app.deps import (
    current_user,
    permission_codes,
    require_permission,
    role_codes,
    write_audit,
)
from app.models import Role, User, user_roles
from app.rbac import DEFAULT_USERS
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["认证与权限"])

USER_WITH_PERMS = selectinload(User.roles).selectinload(Role.permissions)


class LoginIn(BaseModel):
    username: str
    password: str


class UserCreateIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "USER"


class RoleAssignIn(BaseModel):
    role: str


class DemoSwitchIn(BaseModel):
    role: str  # ADMIN / ENGINEER / USER / LEADER


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "roles": role_codes(user),
        "permissions": sorted(permission_codes(user)),
    }


async def load_user(db: AsyncSession, user_id: int) -> User:
    return (
        await db.execute(
            select(User).options(USER_WITH_PERMS).where(User.id == user_id)
        )
    ).scalar_one()


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(
            select(User).options(USER_WITH_PERMS).where(User.username == body.username)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    payload = user_payload(user)
    # 登录本身也是一次操作，先构造返回值再写审计，避免 commit 后 ORM 对象过期
    await write_audit(
        db, user, "auth.login", target=user.username,
        ip=request.client.host if request.client else "",
    )
    return {
        "access_token": create_access_token(user.username, user.id),
        "token_type": "bearer",
        "user": payload,
    }


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return user_payload(user)


@router.post("/demo-switch")
async def demo_switch(
    body: DemoSwitchIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """演示模式的四端身份一键切换：服务端按种子账号签发新令牌，前端无需重复保存密码。"""
    if not settings.demo_mode:
        raise HTTPException(403, "演示身份切换未启用")
    role = (body.role or "").upper()
    seed = next((s for s in DEFAULT_USERS if s[3] == role), None)
    if seed is None:
        raise HTTPException(400, f"未知角色: {role}")
    target = (
        await db.execute(
            select(User).options(USER_WITH_PERMS).where(User.username == seed[0])
        )
    ).scalar_one_or_none()
    if target is None or not target.is_active:
        raise HTTPException(404, "该演示账号不存在或已停用")
    await write_audit(
        db, user, "auth.demo_switch", target=f"{user.username}->{target.username}",
        detail={"from_role": role_codes(user), "to_role": role},
        ip=request.client.host if request.client else "",
    )
    return {
        "access_token": create_access_token(target.username, target.id),
        "token_type": "bearer",
        "user": user_payload(target),
    }


@router.get("/roles")
async def list_roles(
    user: User = Depends(require_permission("role.manage")),
    db: AsyncSession = Depends(get_db),
):
    roles = (
        await db.execute(select(Role).options(selectinload(Role.permissions)))
    ).scalars().all()
    return [
        {
            "code": r.code,
            "name": r.name,
            "description": r.description,
            "permissions": sorted(p.code for p in r.permissions),
        }
        for r in roles
    ]


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreateIn,
    request: Request,
    user: User = Depends(require_permission("user.manage")),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "用户名已存在")
    role = (await db.execute(select(Role).where(Role.code == body.role))).scalar_one_or_none()
    if role is None:
        raise HTTPException(400, f"角色不存在: {body.role}")

    new_user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
    )
    db.add(new_user)
    await db.flush()
    await db.execute(
        insert(user_roles), [{"user_id": new_user.id, "role_id": role.id}]
    )
    await db.commit()
    await write_audit(
        db, user, "user.create", target=body.username,
        detail={"role": body.role}, ip=request.client.host if request.client else "",
    )
    return user_payload(await load_user(db, new_user.id))


@router.get("/users")
async def list_users(
    user: User = Depends(require_permission("user.manage")),
    db: AsyncSession = Depends(get_db),
):
    users = (
        await db.execute(select(User).options(USER_WITH_PERMS).order_by(User.id))
    ).scalars().all()
    return [user_payload(u) for u in users]


@router.put("/users/{user_id}/role")
async def assign_role(
    user_id: int,
    body: RoleAssignIn,
    request: Request,
    user: User = Depends(require_permission("role.assign")),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    role = (await db.execute(select(Role).where(Role.code == body.role))).scalar_one_or_none()
    if role is None:
        raise HTTPException(400, f"角色不存在: {body.role}")
    await db.execute(delete(user_roles).where(user_roles.c.user_id == user_id))
    await db.execute(insert(user_roles), [{"user_id": user_id, "role_id": role.id}])
    await db.commit()
    await write_audit(
        db, user, "role.assign", target=target.username,
        detail={"role": body.role}, ip=request.client.host if request.client else "",
    )
    return user_payload(await load_user(db, user_id))
