# 全容器化 Docker 一键部署

面向两件事：**本机 Mac 一条命令起全栈演示**，以及**同一套编排原样搬上云主机**（阿里云 ECS、腾讯云 CVM、AWS EC2 或任意 VPS）。

开发用的 [`infra/docker-compose.yml`](../../infra/docker-compose.yml) 保持不动（CI 与本地开发继续用它）；生产编排是完全独立的第二套：独立项目名 `ai-native-erp-prod`、独立容器名 `erp_prod_*`、独立命名卷。

## 1. 交付物

| 路径 | 作用 |
| --- | --- |
| [`infra/docker-compose.prod.yml`](../../infra/docker-compose.prod.yml) | 8 个常驻服务 + 2 个 one-shot 作业的完整编排 |
| [`infra/docker/backend.Dockerfile`](../../infra/docker/backend.Dockerfile) | 一个镜像四种角色：`backend` / `worker` / `migrate` / `seed` |
| [`infra/docker/web.Dockerfile`](../../infra/docker/web.Dockerfile) | Next.js 15 `standalone` 产物 + 非 root 运行 |
| [`infra/docker/widget.Dockerfile`](../../infra/docker/widget.Dockerfile) | IIFE 产物 + 构建期 gzip < 35,840 字节门禁，nginx 托管 |
| [`infra/docker/widget-nginx.conf`](../../infra/docker/widget-nginx.conf) | Widget 静态托管：CORS、Cache-Control、`/healthz` |
| [`infra/docker/postgres/10-init-roles.sh`](../../infra/docker/postgres/10-init-roles.sh) | 生产库首次初始化：`app_migrator` / `app_runtime` 双角色 + 扩展 |
| [`infra/docker/nginx.edge.conf`](../../infra/docker/nginx.edge.conf) | 云主机宿主机层反代模板（HTTPS + SSE 不断流） |
| [`.env.prod.example`](../../.env.prod.example) | 无密钥的环境模板；`.env.prod` 已被 git 忽略 |

## 2. 前置要求

- Docker Engine / Docker Desktop 24+，Compose v2.24+（需要 `service_completed_successfully` 与 `profiles`）
- 建议 ≥ 6 GB 内存（Next.js 构建阶段会占用 2 GB 左右）
- 首次构建需要能访问 `pypi.org`、`registry.npmjs.org`、`ghcr.io`、Docker Hub
- 本机演示需要空闲端口 `3000/3001/8000/5433/6432/6379/9000/9001`

> 端口冲突：所有基础设施端口默认只绑 `127.0.0.1`，并且 Postgres 用 `5433`。若本机还跑着开发栈，先 `docker compose -f infra/docker-compose.yml down`（保留数据卷），再启动生产栈。**不要停止或改动与本项目无关的其他容器。**

## 3. Mac 本机：5 分钟一键启动

```bash
cd /path/to/ai-native-erp

# 1) 复制模板（.env.prod 已被 .gitignore 忽略，永不入库）
cp .env.prod.example .env.prod

# 2) 生成强随机密钥并写回（POSIX awk，macOS 与 Linux 通用）
set_secret() {
  awk -v k="$1" -v v="$2" 'index($0, k"=")==1 {print k"="v; next} {print}' \
    .env.prod > .env.prod.tmp && mv .env.prod.tmp .env.prod
}
set_secret POSTGRES_PASSWORD       "$(openssl rand -hex 24)"
set_secret APP_MIGRATOR_PASSWORD   "$(openssl rand -hex 24)"
set_secret APP_RUNTIME_PASSWORD    "$(openssl rand -hex 24)"
set_secret S3_ACCESS_KEY           "erp$(openssl rand -hex 8)"
set_secret S3_SECRET_KEY           "$(openssl rand -hex 24)"
set_secret MASTER_KEK_HEX          "$(openssl rand -hex 32)"
set_secret JWT_SECRET              "$(openssl rand -hex 48)"

# 3) 构建并启动（demo-seed 会灌入演示业务数据）
docker compose -f infra/docker-compose.prod.yml --env-file .env.prod \
  --profile demo-seed up -d --build

# 4) 确认状态：8 个服务 healthy，migrate/seed 退出码 0
docker compose -f infra/docker-compose.prod.yml --env-file .env.prod ps -a
```

口令必须是 **hex**（不要 `#`、`@`、`/`、`:`）：`APP_RUNTIME_PASSWORD` 会拼进 PgBouncer 的 `DATABASE_URL`。

启动顺序由编排保证：

```
postgres(healthy) → pgbouncer(healthy) → migrate(exit 0) → seed(exit 0)
                     redis(healthy) ┐
                     minio(healthy) ┴→ backend(healthy) → web  → widget
                                      └→ worker(healthy)
```

`seed` 带 `profiles: ["demo-seed"]`，**生产环境不带该 profile 就永远不会灌测试数据**。

## 4. 访问地址与演示账号

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 工作台 | <http://localhost:3000/login> | Next.js UI + Auth/SSE BFF |
| API 文档 | <http://localhost:8000/docs> | FastAPI Swagger |
| API 健康 | <http://localhost:8000/healthz>、`/readyz` | `/readyz` 返回 `postgres: ok`, `redis: ok` |
| Widget | <http://localhost:3001/healthz> | 静态 IIFE，`/livechat-widget.iife.js` |

演示空间 `tenant_slug = quanzhou-led-factory`，密码统一 `FactoryPass2026!`：

| 角色 | 邮箱 | 能力 |
| --- | --- | --- |
| Owner | `owner@quanzhou-led.example.com` | 全量，含成本/利润 |
| Admin | `admin@quanzhou-led.example.com` | 全量，含成本/利润 |
| Manager | `manager@quanzhou-led.example.com` | 全量，含成本/利润 |
| Agent | `agent@quanzhou-led.example.com` | 收件箱/报价，**看不到成本** |
| Finance | `finance@quanzhou-led.example.com` | 财务、CVP、结算核销 |

第二个演示租户 `shenzhen-audio-dtc`（`owner@shenzhen-audio.example.com`）用于多租户隔离演示。

命令行冒烟（与浏览器等价，走 Web 层 → BFF → FastAPI → PgBouncer → Postgres）：

```bash
curl -s -c /tmp/c.txt -X POST http://127.0.0.1:3000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@quanzhou-led.example.com","password":"FactoryPass2026!","tenant_slug":"quanzhou-led-factory"}'

curl -s -b /tmp/c.txt http://127.0.0.1:3000/api/bff/v1/inbox/conversations?limit=5
curl -s -b /tmp/c.txt http://127.0.0.1:3000/api/bff/v1/catalog/products?limit=5
curl -s -b /tmp/c.txt http://127.0.0.1:3000/api/bff/v1/orders/warehouses
curl -sN -b /tmp/c.txt -H 'Accept: text/event-stream' \
  http://127.0.0.1:3000/api/bff/v1/inbox/stream    # 期待 event: connected
```

`/api/auth/login` 是 `/api/bff/v1/auth/login` 的运维别名（在 `apps/web/next.config.ts` 用 rewrite 声明）；UI 自身只用 BFF 路径。

## 5. 容器与卷

| 服务 | 镜像 | 端口（宿主） | 说明 |
| --- | --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg17` | `127.0.0.1:5433` | 卷 `erp_prod_pg_data`，首启执行角色初始化脚本 |
| `pgbouncer` | `edoburu/pgbouncer:latest` | `127.0.0.1:6432` | 事务池；配置由 `DATABASE_URL` 环境变量生成，不含明文口令 |
| `redis` | `redis:7-alpine` | `127.0.0.1:6379` | AOF + `noeviction`，卷 `erp_prod_redis_data` |
| `minio` | `rustfs/rustfs:latest` | `127.0.0.1:9000/9001` | S3 兼容，卷 `erp_prod_s3_data` |
| `migrate` | 本仓库 backend 镜像 | — | `alembic upgrade head`，成功即退出 |
| `seed` | 同上 | — | `--profile demo-seed` 才运行 |
| `backend` | 同上 | `127.0.0.1:8000` | uvicorn，`HEALTHCHECK` 打 `/healthz` |
| `worker` | 同上 | — | `python -m erp_core.worker_main`（Redis Streams 消费） |
| `web` | 本仓库 web 镜像 | `127.0.0.1:3000` | `BFF_BACKEND_URL=http://backend:8000` 运行时注入 |
| `widget` | 本仓库 widget 镜像 | `127.0.0.1:3001` | nginx 静态托管 |

`PROD_BIND_ADDRESS=127.0.0.1` 是安全默认值：容器端口默认不对公网暴露，公网入口只走宿主机 Nginx 或云负载均衡（见第 8 节）。

## 6. 日常运维

```bash
COMPOSE="docker compose -f infra/docker-compose.prod.yml --env-file .env.prod"

$COMPOSE logs -f backend worker          # 跟踪日志（json-file 轮转 10MB×3）
$COMPOSE restart backend                 # 只重启单个服务
$COMPOSE ps -a                           # 含 migrate/seed 的退出码
$COMPOSE up -d --build                   # 代码更新后重建并滚动替换
$COMPOSE down                            # 停止（保留数据卷）
$COMPOSE down -v                         # 停止并删除数据卷（不可恢复，慎用）
```

**迁移与发版**：新增 Alembic 版本后照常 `up -d --build`，`migrate` 会跑完再放行 `backend`/`worker`；失败时 `backend` 不会启动（fail-closed）。

**演示数据策略**：生产不带 `--profile demo-seed`；本机重新灌数据用
`$COMPOSE --profile demo-seed run --rm seed`（幂等，已存在产品的空间会跳过）。

**Postgres 备份 / 恢复**：

```bash
docker exec erp_prod_postgres pg_dump -U postgres -Fc erp_saas > erp-$(date +%F).dump
docker exec -i erp_prod_postgres pg_restore -U postgres -d erp_saas --clean --if-exists < erp-2026-11-19.dump
```

建议在云主机上挂 cron 每日备份到对象存储，并每季度做一次恢复演练（备份没演练过等于没有）。

## 7. 接入真实 AI / 渠道凭据

编辑 `.env.prod` 后 `$COMPOSE up -d backend worker`：

```ini
# 任一即可，未填则严格走确定性工具链（不产生 LLM 调用）
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...

# 渠道
WHATSAPP_APP_ID=...
WHATSAPP_APP_SECRET=...
WHATSAPP_VERIFY_TOKEN=<自定义 32+ 随机串>
SHOPIFY_CLIENT_ID=...
SHOPIFY_CLIENT_SECRET=...
TRACK17_API_KEY=...
```

要点：

- 密钥只进容器环境变量，绝不进镜像；`MASTER_KEK_HEX` 用于信封加密（渠道 Token、IMAP/SMTP、BYOK），**每个租户一把 DEK**。
- LLM 只做意图/措辞层，价格、MOQ、库存、订单、物流仍由 `ErpToolRegistry` 确定性工具取数并带 `source_tool_call_id`；无 Key 也能跑业务。
- 轮换 `MASTER_KEK_HEX` 时同时改 `KMS_KEY_ID`，旧数据用旧版本号可解，再跑一次重加密任务；密钥备份到 KMS/密钥管理服务，不要放在同一台机器上。

## 8. 公网 Webhook 联调

WhatsApp / Shopify / 17TRACK 都需要一个 HTTPS 回调地址。两种做法：

### A. Cloudflare Tunnel（最快，无需公网 IP）

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create erp-dev
cloudflared tunnel route dns erp-dev api.erp.example.com

cat > ~/.cloudflared/config.yml <<'YAML'
tunnel: erp-dev
credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: api.erp.example.com
    service: http://127.0.0.1:8000
  - hostname: erp.erp.example.com
    service: http://127.0.0.1:3000
  - hostname: widget.erp.example.com
    service: http://127.0.0.1:3001
  - service: http_status:404
YAML

cloudflared tunnel run erp-dev
```

然后在各平台后台填：

| 平台 | 回调 URL |
| --- | --- |
| WhatsApp Cloud API | `https://api.erp.example.com/webhooks/whatsapp` |
| Shopify | `https://api.erp.example.com/api/v1/webhooks/shopify` |
| 17TRACK | `https://api.erp.example.com/webhooks/17track` |
| Resend 入站邮件 | `https://api.erp.example.com/webhooks/resend` |

Worker 必须在线（`erp_prod_worker`），否则 webhook 只入库不投递。

### B. 云主机 + Nginx + Let's Encrypt

把 [`infra/docker/nginx.edge.conf`](../../infra/docker/nginx.edge.conf) 复制到 `/etc/nginx/conf.d/erp.conf`，替换域名，`certbot --nginx -d ...` 签证书。该模板已经为 SSE 设好
`proxy_buffering off; proxy_cache off; proxy_read_timeout 3600s;`（`/api/bff/v1/inbox/stream` 与 `/api/v1/inbox/stream`），并且对 `/webhooks/` 单独放宽限速，保证 200 在 100ms 内返回。

## 9. 云主机上线前安全检查清单

- [ ] **所有密钥换过一遍**：`MASTER_KEK_HEX`、`JWT_SECRET`、Postgres/PgBouncer/S3 口令，全部 `openssl rand`；`.env.prod` 权限 `chmod 600`，只 root 可读
- [ ] **不启用 demo 数据**：`up` 时不要带 `--profile demo-seed`；确认库里没有演示账号（`SELECT count(*) FROM users`）
- [ ] **端口不裸奔**：`PROD_BIND_ADDRESS=127.0.0.1`，安全组只放 80/443（+ 运维跳板），5433/6432/6379/9000 绝不对外开放
- [ ] **TLS + SSE**：Nginx 模板照抄，`nginx -t` 通过；用 `curl -N` 验证流式不被缓冲
- [ ] **RLS 没被绕过**：`app_runtime` 必须是 `NOSUPERUSER NOBYPASSRLS`（初始化脚本已强制），`DATABASE_RUNTIME_URL` 只能走 `pgbouncer:6432`
- [ ] **备份**：每日 `pg_dump -Fc` 到异地对象存储，保留 ≥ 30 天，季度恢复演练
- [ ] **KMS 轮换**：`MASTER_KEK_HEX` 存 KMS/密钥管理，按季度轮换并保留旧版本用于解密
- [ ] **日志与监控**：json-file 已轮转；建议采集 `/healthz`、`/readyz`、`erp_prod_*` 退出码，配告警
- [ ] **镜像可追溯**：推送私有 registry 后用 `ERP_IMAGE_TAG=<git-sha>` 固定版本，生产不要重新构建
- [ ] **资源与重启**：`restart: unless-stopped` 已开；需要时用 compose `deploy.resources` 限制内存

## 10. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `service "migrate" didn't complete successfully` | `docker logs erp_prod_migrate`；常见是 `permission denied for schema public`（数据卷是旧的不完整初始化，`down -v` 重建）或迁移脚本报错 |
| `SettingsError ... CORS_ORIGINS` | 老版本已用 `NoDecode` 修复；若仍出现请确认镜像已重建 |
| `pgbouncer` 不 healthy | `APP_RUNTIME_PASSWORD` 含非 hex 字符导致 `DATABASE_URL` 解析失败；改 hex 后 `down -v` 或同步改库内角色口令 |
| 端口被占用 | `lsof -nP -iTCP:5433 -sTCP:LISTEN`；先停开发栈，**不要**动无关容器 |
| 浏览器登录后立刻跳回登录页 | `NODE_ENV=production` 下 Cookie 带 `Secure`；`http://localhost` 被浏览器视为安全上下文可用，但用局域网 IP/裸 IP 访问时需要 HTTPS |
| 收件箱不刷新 | 反代缓冲了 SSE；确认 `proxy_buffering off` 与 `X-Accel-Buffering: no`，或直连 `127.0.0.1:8000/api/v1/inbox/stream` 对照 |
| Widget 不加载 | 确认 `3001` 可访问且响应带 `Access-Control-Allow-Origin: *`；Shopify 里填的是完整 `https://.../livechat-widget.iife.js` |
| `pnpm build` 卡住 / OOM | 给 Docker 至少 6 GB 内存；构建阶段 Next.js 会并行压缩 |

## 11. 质量门禁（提交前自查）

```bash
pnpm --filter web lint && pnpm --filter web typecheck && pnpm --filter web test && pnpm --filter web build
pnpm --filter widget build
gzip -c apps/widget/dist/livechat-widget.iife.js | wc -c      # < 35840
uv run ruff check apps/backend && uv run mypy apps/backend/src
E2E_MODE=mock pnpm --filter e2e test
```

镜像层等价校验：

```bash
docker build -f infra/docker/backend.Dockerfile -t ai-native-erp/backend:check .
docker build -f infra/docker/web.Dockerfile     -t ai-native-erp/web:check .
docker build -f infra/docker/widget.Dockerfile  -t ai-native-erp/widget:check .
```
