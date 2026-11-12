# OpsAI · AI 智能排障平台

一个面向运维排障场景的四端协作演示系统：普通用户提问并更新工作日志，工程师接单处置，管理员查看提问人身份并发起私聊，领导查看审计与全过程记录。所有操作都会落入**操作台账、工作日志与审计记录**，四端身份可随时切换，聊天记录按账号严格隔离。

> **安全须知（请务必先读）**
> 这是一个**演示/原型**项目。默认启用 `DEMO_MODE`（已登录用户可一键切换四端身份）、模拟数据源和弱口令演示账号。
> **不要直接把默认配置暴露到公网。** 对外部署前必须：更换 `JWT_SECRET`、更换 `OPSAI_DEMO_PASSWORD`、关闭 `DEMO_MODE`、接入真实数据源并配置 HTTPS 与访问控制。

## 功能一览

- **四端协作**：管理员 / 工程师 / 普通用户 / 领导，各自独立的视图与权限矩阵（RBAC）
- **AI 专业问答**：只受理服务器排障类问题，非专业问题统一回复「系统目前不支持该类对话」；未配置大模型 Key 时自动降级为意图感知规则模式，功能仍可完整演示
- **排障工作流**：探测 → 条件分支 → 并行处置 → 重启服务，状态机驱动
- **真实留痕**：操作台账、工作日志、审计日志、全局动态，每次动作三重留痕
- **Agent 直接处置**：管理员 / 工程师下达「帮我清理内存」等命令时，Agent 先说明目标、命令与风险并询问确认，确认后输出逐行执行结果，自动生成工单与三重留痕；取消、高危命令、无权限角色都有明确反馈
- **相同问题合并报修**：同服务器同一问题自动合并到一张未办结工单，工单上列出全部报修人，合并动作写入工作日志与审计
- **管理员私聊**：可见提问人身份，支持双向互动与未读提醒
- **聊天隔离**：四端对话记录按 `user_id` 严格隔离，提问前历史为空
- **本地时间**：服务端统一 Asia/Shanghai 时间，前端与服务器时钟同步
- **单端口交付**：FastAPI 同时提供 API 与前端静态资源，一个 8000 端口即可访问

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13、FastAPI、SQLAlchemy 2.0 (async)、asyncpg、PostgreSQL 16、Redis、JWT、Pydantic Settings |
| AI | LangChain + DashScope / Qwen 适配层（可选），未配置时规则降级 |
| 前端 | Vue 3、Vite、Element Plus、Axios，白 / 灰 / 蓝主题 |
| 可观测性 | Prometheus metrics、OpenTelemetry、Grafana / ELK / Jaeger 配置样例 |
| 部署 | Docker Compose 多阶段构建、Nginx 反代样例、Ansible inventory 样例 |

## 快速开始（Docker Compose，推荐）

```bash
git clone https://github.com/gushjing/opsai-platform.git
cd opsai-platform
cp .env.example .env
# 编辑 .env：至少修改 JWT_SECRET 和 OPSAI_DEMO_PASSWORD
docker compose up -d --build
```

打开 `http://localhost:8000`。首次启动会自动建表并写入演示账号与默认工作流。

## 手动本地运行

需要 Python 3.13+、Node.js 20+、PostgreSQL 16、Redis 7。

```bash
# 1) 后端依赖
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# 2) 配置
cp .env.example .env          # 按本机地址修改 DATABASE_URL / REDIS_URL
export OPSAI_DIST="$(pwd)/frontend/dist"   # Windows: set OPSAI_DIST=...

# 3) 前端构建
cd frontend && npm ci && npm run build && cd ..

# 4) 启动（单端口同时提供 API 与前端）
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

后端源码改动需要重启 uvicorn；前端 `src/` 改动需要重新 `npm run build`（页面加载的是 `dist/` 产物），然后强制刷新浏览器。

## 演示账号

四个角色使用**同一个演示口令**，取自环境变量 `OPSAI_DEMO_PASSWORD`（默认 `OpsAI@Demo2026`）：

| 角色 | 用户名 | 主要能力 |
|---|---|---|
| 管理员 | `admin` | 全局管理、查看提问人身份、发起私聊 |
| 工程师 | `engineer` | 故障排查、接单处置、写工作日志 |
| 普通用户 | `user` | 发起提问、操作服务器、更新工作日志 |
| 领导 | `leader` | 审计与报表、全过程可见 |

> 口令在 `backend/app/rbac.py` 中由环境变量注入，登录页由 `VITE_DEMO_PASSWORD` 注入，源码中不含真实凭据。

## 配置项

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy async 连接串 | `postgresql+asyncpg://opsai:opsai_dev@127.0.0.1:5432/opsai` |
| `REDIS_URL` | Redis 连接串 | `redis://127.0.0.1:6379/0` |
| `JWT_SECRET` | 签名密钥，**部署必改** | 开发占位值 |
| `OPSAI_DEMO_PASSWORD` | 四个演示账号的初始口令 | `OpsAI@Demo2026` |
| `DEMO_MODE` | 允许四端一键切换身份 | `true` |
| `USE_MOCK_DATASOURCE` | 使用带故障特征的模拟数据 | `true` |
| `DASHSCOPE_API_KEY` | 大模型 Key，留空则规则降级 | 空 |
| `OPSAI_DIST` | 前端构建产物路径 | `<repo>/frontend/dist` |
| `OPSAI_ENV_FILE` | 配置文件路径 | `<repo>/.env` |

完整样例见 [`.env.example`](.env.example)。

## 目录结构

```
opsai-platform/
├── backend/
│   ├── app/                 # FastAPI 应用：路由、模型、RBAC、Agent、工作流、审计
│   └── requirements.txt
├── frontend/
│   ├── src/                 # Vue 3 视图、API 封装、主题样式
│   ├── index.html
│   └── vite.config.js
├── config/                  # Prometheus / Logstash / Nginx / Ansible 配置样例
├── observability/           # 可观测性组件配置样例
├── scripts/smoke_test.py    # 部署后冒烟自检
├── docs/
│   ├── DEPLOYMENT.md        # 部署、常驻运行与验证
│   └── ARCHITECTURE.md      # 架构与数据流
├── Dockerfile               # 前端构建 + 后端运行的多阶段镜像
├── docker-compose.yml
└── .env.example
```

## 冒烟自检

```bash
python scripts/smoke_test.py --base http://localhost:8000
```

会检查服务元数据、演示账号登录、四端接口可达性与聊天隔离。

## 文档

- [部署与常驻运行](docs/DEPLOYMENT.md)
- [架构与数据流](docs/ARCHITECTURE.md)

## 许可

本仓库暂未指定开源许可证，代码仅用于演示与学习；如需再分发或商用，请先与仓库所有者确认。
