# 部署与运行

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.13+ | 后端运行时 |
| Node.js | 20+ | 前端构建（仅构建阶段需要） |
| PostgreSQL | 16 | 业务数据 |
| Redis | 7 | 会话 / 缓存，使用 RESP2 协议 |
| Docker | 24+ / Compose v2 | 可选，一键部署推荐 |

大模型 Key 不是必需项：未配置 `DASHSCOPE_API_KEY` 时，AI 问答自动降级为意图感知规则模式，仍只回答排障类专业问题。

## 方式一：Docker Compose

```bash
cp .env.example .env
# 修改 JWT_SECRET / OPSAI_DEMO_PASSWORD；公网部署把 DEMO_MODE 设为 false
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

`Dockerfile` 是多阶段构建：Node 阶段产出 `frontend/dist`，Python 阶段安装后端依赖并把静态资源放进 `/app/frontend/dist`，最终由 uvicorn 在 8000 端口同时提供 API 和页面。

`app` 服务会等待 PostgreSQL 健康检查通过再启动，避免「database system is starting up」导致的启动失败。

数据落在命名卷 `pgdata` / `redisdata` 中，`docker compose down` 不会删除数据。

## 方式二：手动运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env

cd frontend && npm ci && npm run build && cd ..

export OPSAI_DIST="$(pwd)/frontend/dist"     # Windows: set OPSAI_DIST=...
export OPSAI_DEMO_PASSWORD='YourDemoPassword' # 需与 .env 保持一致
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

手动运行时 `.env` 由 Pydantic Settings 读取，但 `OPSAI_DEMO_PASSWORD` 由后端通过环境变量读取，因此需要显式 `export`（Docker Compose 会通过 `env_file` 自动注入）。

## 代码改动如何生效

| 改动位置 | 生效方式 |
|---|---|
| `backend/app/*.py` | 重启 uvicorn（默认未开启 `--reload`） |
| `frontend/src/**` | `cd frontend && npm run build`，然后浏览器强制刷新 |
| `frontend/dist/**` | 刷新即可，但下次构建会被覆盖 |
| `.env` | 重启服务 |
| Docker 部署下的任何改动 | `docker compose up -d --build` |

开发期如需热更新，可用 `uvicorn ... --reload` 配合 `npm run dev`（Vite 开发服务器需配置 `/api` 代理）。

## 常驻运行（本机长期分享）

应用本身只是一个 uvicorn 进程，是否常驻取决于你怎么托管它。推荐顺序：

1. **容器化**：`restart: unless-stopped` + Docker 开机自启，最简单可靠。
2. **进程管理器**：Linux 用 systemd unit，Windows 用任务计划 / 服务封装。
3. **独立守护进程**：一个脱离终端的 watchdog，周期性检查各组件端口与 `/api/meta`，异常时自动拉起；停止时用 `pg_ctl -m fast stop` 正常关闭数据库，避免下次启动做崩溃恢复。

对外分享还需要一层反向代理或隧道：

- 内网：直接访问 `http://<主机IP>:8000`
- 公网：Nginx 反代 + HTTPS，或临时隧道（如 localtunnel / cloudflared）。隧道子域名可能被回收，生产环境应使用固定域名。
- 若用隧道且要固定子域名，客户端需要在拿到随机域名时重试，否则分享链接会失效。

## 验证

```bash
python scripts/smoke_test.py --base http://localhost:8000
```

依次检查服务元数据、四个演示账号登录、以及各账号聊天历史接口可达。接口清单见 FastAPI 自带文档 `/docs`。

## 上线前检查清单

- [ ] `JWT_SECRET` 换成足够长的随机值
- [ ] `OPSAI_DEMO_PASSWORD` 更换，或直接删除演示账号改用真实用户体系
- [ ] `DEMO_MODE=false`（关闭四端一键切换）
- [ ] `USE_MOCK_DATASOURCE=false` 并登记真实数据源
- [ ] PostgreSQL / Redis 启用认证，限制端口仅内网可达
- [ ] 配置 HTTPS、反向代理与访问日志
- [ ] 配置备份（数据库定时快照）与监控告警
