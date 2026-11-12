"""RBAC 权限种子数据，严格对应 PRD 2.1.6 的四角色权限矩阵。"""
import os

from sqlalchemy import delete, insert, select, text

from app.db import Base, SessionLocal, engine
from app.models import Permission, Role, User, role_permissions, user_roles
from app.security import hash_password

PERMISSIONS = [
    ("chat.ask", "发送自然语言问句", "对话交互"),
    ("chat.view", "查看排障结果", "对话交互"),
    ("chat.followup", "多轮对话追问", "对话交互"),
    ("chat.manage", "对话记录管理", "对话交互"),
    ("metrics.view", "查看监控指标", "运维数据"),
    ("metrics.overview", "查看指标概览", "运维数据"),
    ("logs.view", "查看原始日志", "运维数据"),
    ("trace.view", "查看链路追踪", "运维数据"),
    ("datasource.manage", "配置数据源连接", "运维数据"),
    ("ops.execute", "执行服务器操控命令", "自动化运维"),
    ("workflow.manage", "编排/执行工作流", "自动化运维"),
    ("audit.view", "操作审计查看", "自动化运维"),
    ("user.manage", "用户管理", "权限管理"),
    ("role.manage", "角色权限配置", "权限管理"),
    ("role.assign", "角色分配", "权限管理"),
    ("system.config", "系统参数配置", "系统设置"),
    ("report.view", "查看运维报表", "报表统计"),
    ("report.export", "导出报表", "报表统计"),
    ("operation.create", "发起服务器处理工单", "协同处置"),
    ("operation.view", "查看操作台账", "协同处置"),
    ("operation.handle", "接单/状态流转/执行处置", "协同处置"),
    ("worklog.write", "更新工作日志", "协同处置"),
    ("activity.view", "查看全局操作动态", "协同处置"),
    ("inquiry.view", "查看提问人身份", "协同处置"),
    ("private.chat", "参与一对一私聊", "协同处置"),
]

ALL_CODES = [p[0] for p in PERMISSIONS]

ROLE_MATRIX = {
    "ADMIN": ("管理员", "系统配置、用户管理、数据源管理、角色权限分配等全局管理", ALL_CODES),
    "ENGINEER": (
        "工程师",
        "故障排查、服务器操作、监控查看、日志分析",
        [
            "chat.ask", "chat.view", "chat.followup", "chat.manage",
            "metrics.view", "metrics.overview", "logs.view", "trace.view",
            "ops.execute", "workflow.manage", "report.view", "report.export",
            "operation.create", "operation.view", "operation.handle",
            "worklog.write", "activity.view", "private.chat",
        ],
    ),
    "USER": (
        "普通用户",
        "发起处理工单、更新工作日志、查看概览与排障报告",
        [
            "chat.ask", "chat.view", "chat.followup", "metrics.overview", "report.view",
            "operation.create", "operation.view", "worklog.write", "activity.view",
            "private.chat",
        ],
    ),
    "LEADER": (
        "领导",
        "查看全局操作动态、审计与报表，可批注工作日志，不可执行运维操作",
        [
            "chat.ask", "chat.view", "chat.followup", "metrics.overview",
            "audit.view", "report.view", "report.export",
            "operation.create", "operation.view", "worklog.write", "activity.view",
            "private.chat",
        ],
    ),
}

DEMO_PASSWORD = os.getenv("OPSAI_DEMO_PASSWORD", "OpsAI@Demo2026")
DEFAULT_USERS = [
    ("admin", DEMO_PASSWORD, "系统管理员", "ADMIN"),
    ("engineer", DEMO_PASSWORD, "运维工程师", "ENGINEER"),
    ("user", DEMO_PASSWORD, "普通用户", "USER"),
    ("leader", DEMO_PASSWORD, "领导", "LEADER"),
]


async def init_db() -> None:
    """建表并写入权限种子。

    关联关系一律用关联表原生 insert，避免异步 ORM 中给 relationship 赋值
    触发隐式 lazyload（MissingGreenlet）。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 轻量增量迁移：老库补齐「报修人合并」相关字段（幂等，不动既有数据）
        for ddl in (
            "ALTER TABLE operation_records ADD COLUMN IF NOT EXISTS reporters TEXT DEFAULT ''",
            "ALTER TABLE operation_records ADD COLUMN IF NOT EXISTS reporter_ids VARCHAR(512) DEFAULT ''",
            "ALTER TABLE operation_records ADD COLUMN IF NOT EXISTS issue_key VARCHAR(128) DEFAULT ''",
        ):
            await conn.execute(text(ddl))

    async with SessionLocal() as db:
        # 增量升级：老库里已存在的权限不重复写，只补齐后加的权限点
        existing_codes = {
            p.code for p in (await db.execute(select(Permission))).scalars().all()
        }
        missing = [
            Permission(code=code, name=name, module=module)
            for code, name, module in PERMISSIONS
            if code not in existing_codes
        ]
        if missing:
            db.add_all(missing)
            await db.commit()

        perm_map = {
            p.code: p.id
            for p in (await db.execute(select(Permission))).scalars().all()
        }
        role_ids = {
            r.code: r.id for r in (await db.execute(select(Role))).scalars().all()
        }

        for code, (name, desc, codes) in ROLE_MATRIX.items():
            if code not in role_ids:
                role = Role(code=code, name=name, description=desc)
                db.add(role)
                await db.flush()
                role_ids[code] = role.id
            await db.execute(
                delete(role_permissions).where(
                    role_permissions.c.role_id == role_ids[code]
                )
            )
            rows = [
                {"role_id": role_ids[code], "permission_id": perm_map[c]}
                for c in codes if c in perm_map
            ]
            if rows:
                await db.execute(insert(role_permissions), rows)
        await db.commit()

        existing_users = {
            u.username for u in (await db.execute(select(User))).scalars().all()
        }
        for username, password, display_name, role_code in DEFAULT_USERS:
            if username not in existing_users:
                user = User(
                    username=username,
                    password_hash=hash_password(password),
                    display_name=display_name,
                )
                db.add(user)
                await db.flush()
                await db.execute(
                    insert(user_roles),
                    [{"user_id": user.id, "role_id": role_ids[role_code]}],
                )
        await db.commit()
