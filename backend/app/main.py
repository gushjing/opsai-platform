"""OpsAI API entry point: serves the API and the built frontend on one port."""
import json
import os
import platform
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, get_db
from app.deps import get_redis
from app.models import AuditLog, WorkflowDef
from app.rbac import init_db
from app.routers_admin import router as admin_router
from app.routers_auth import router as auth_router
from app.routers_chat import router as chat_router
from app.routers_ops import router as ops_router
from app.routers_private import router as private_router
from app.security import decode_access_token
from app.timeutil import now_local, server_time_info

DIST = Path(os.getenv("OPSAI_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))

# 预置工作流：探测 → 条件分支 → 并行处置 → 重启服务
DEFAULT_WORKFLOW = {
    "start": "start",
    "nodes": [
        {"id": "start", "type": "start", "next": "probe"},
        {"id": "probe", "type": "action", "action": "query_server",
         "params": {"server": "0931"}, "next": "check"},
        {"id": "check", "type": "condition", "expression": "disk_usage > 90",
         "branches": {"true": "parallel_fix", "false": "notify_ok"}},
        {"id": "parallel_fix", "type": "parallel", "nodes": ["clean", "notify_fix"],
         "next": "restart"},
        {"id": "clean", "type": "action", "action": "clean_disk",
         "params": {"released_gb": 12.5}},
        {"id": "notify_fix", "type": "action", "action": "notify",
         "params": {"channel": "wecom", "message": "0931 磁盘水位超阈值，已启动自动处置"}},
        {"id": "restart", "type": "action", "action": "restart_service",
         "params": {"service": "order-service"}, "next": "end"},
        {"id": "notify_ok", "type": "action", "action": "notify",
         "params": {"channel": "wecom", "message": "0931 磁盘水位正常"}, "next": "end"},
        {"id": "end", "type": "end"},
    ],
}


async def seed_default_workflow() -> None:
    async with SessionLocal() as db:
        exists = (
            await db.execute(
                select(WorkflowDef).where(WorkflowDef.name == "磁盘空间自动处置")
            )
        ).scalar_one_or_none()
        if not exists:
            db.add(
                WorkflowDef(
                    name="磁盘空间自动处置",
                    definition_json=json.dumps(DEFAULT_WORKFLOW, ensure_ascii=False),
                )
            )
            await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_default_workflow()
    yield


app = FastAPI(title="智能排障平台 API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def audit_every_write(request: Request, call_next):
    """兜底审计：所有写请求一律进系统日志，即使业务路由漏写也不会丢记录。

    业务语义动作由各路由显式 write_audit 记录；这里补一条访问级 request.write，
    形成「业务操作日志 + 访问日志」双留痕。
    """
    response = await call_next(request)
    path = request.url.path
    if (
        request.method in WRITE_METHODS
        and path.startswith("/api/")
        and path != "/api/auth/login"
    ):
        user = getattr(request.state, "user", None)
        username = getattr(user, "username", "") if user is not None else ""
        user_id = getattr(user, "id", 0) if user is not None else 0
        if not username:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                try:
                    username = str(
                        decode_access_token(auth.split(" ", 1)[1]).get("sub") or ""
                    )
                except Exception:
                    username = ""
        detail = {
            "method": request.method, "path": path,
            "status_code": response.status_code,
        }
        try:
            async with SessionLocal() as db:
                db.add(
                    AuditLog(
                        user_id=user_id,
                        username=username or "anonymous",
                        action="request.write",
                        target=path,
                        detail_json=json.dumps(detail, ensure_ascii=False),
                        ip=request.client.host if request.client else "",
                        result="success" if response.status_code < 400 else "failed",
                        created_at=now_local(),
                    )
                )
                await db.commit()
        except Exception:
            pass  # 审计失败不应影响业务响应
    return response


app.mount("/metrics", make_asgi_app())

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(ops_router)
app.include_router(private_router)


@app.get("/api/meta")
async def meta():
    return {
        "version": "1.2.0",
        "python": platform.python_version(),
        "llm": settings.qwen_model if settings.dashscope_api_key else "未配置（意图感知规则降级模式）",
        "datasource_mode": "mock" if settings.use_mock_datasource else "real",
        "rbac_roles": ["ADMIN", "ENGINEER", "USER", "LEADER"],
        "domain_guard": "仅受理服务器排障专业问题",
        "collaboration": "操作台账 / 工作日志 / 全局动态 / 管理员私聊",
        "chat_isolation": "四端对话记录严格按账号隔离",
        "demo_mode": settings.demo_mode,
        "private_chat": "管理员可查看提问人身份并发起双向私聊",
        **server_time_info(),
    }


@app.get("/api/health")
async def health():
    checks = {}
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"unavailable ({type(exc).__name__})"

    try:
        import asyncpg

        dsn = settings.database_url.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn, timeout=2)
        await conn.fetchval("SELECT 1")
        await conn.close()
        checks["postgresql"] = "ok"
    except Exception as exc:
        checks["postgresql"] = f"unavailable ({type(exc).__name__})"

    checks["llm_api_key"] = "configured" if settings.dashscope_api_key else "not configured"
    healthy = checks["redis"] == "ok" and checks["postgresql"] == "ok"
    return {
        "status": "ok" if healthy else "degraded",
        "python": platform.python_version(),
        "checks": checks,
    }


@app.get("/api")
async def api_info():
    return {"service": "ops-ai-backend", "version": "1.2.0", "docs": "/docs",
            "frontend": "/"}


# ---------- 单端口托管前端：局域网用户直接访问根路径即可使用 ----------

def _index() -> FileResponse | JSONResponse:
    index = DIST / "index.html"
    if not index.exists():
        return JSONResponse(
            {"error": "前端尚未构建，请先执行 npm run build"}, status_code=503
        )
    return FileResponse(index)


@app.get("/", include_in_schema=False)
async def spa_root():
    return _index()


@app.get("/app", include_in_schema=False)
async def spa_alias():
    return _index()


if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")
