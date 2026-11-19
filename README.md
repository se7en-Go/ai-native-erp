# ai-native-erp

面向工贸一体企业的多租户 B2B + B2C 跨境电商全渠道 AI 工作台与轻量 ERP。B2B 询盘、报价与 B2C 零售售后共用收件箱、商品、订单和业财底座。旗舰演示预设为 **Quanzhou Smart LED Lighting Factory**（`hybrid_factory_b2b_b2c`）。

## 仓库结构

| 路径 | 职责 |
| --- | --- |
| [`apps/backend`](apps/backend/) | FastAPI 模块化单体、Taskiq 任务、Alembic 迁移、种子脚本与后端测试 |
| [`apps/web`](apps/web/) | Next.js 15 工作台、薄 Auth/SSE BFF 与 OpenAPI 客户端 |
| [`apps/widget`](apps/widget/) | Vite WebChat IIFE bundle、Shopify Theme App Extension 的 Embed Block |
| [`contracts`](contracts/) | FastAPI 导出的 [`openapi.json`](contracts/openapi.json) |
| [`infra`](infra/) | 本地 Compose、数据库角色、PgBouncer 与 import-linter 配置 |
| [`tests/e2e`](tests/e2e/) | Chromium：B2B 报价到 CVP、B2C 严格检索与 RBAC、入驻流程 |
| [`docs/compliance`](docs/compliance/) | Day-1 送审准备材料与演示知识库种子包 |
| [`docs/superpowers`](docs/superpowers/) | 架构总规格、共享契约与分阶段实施计划 |

## 架构与安全约束

- **租户隔离**：租户数据由 PostgreSQL 17 `ENABLE` + `FORCE ROW LEVEL SECURITY` 保护。应用业务查询以非超级用户 `app_runtime` 通过 PgBouncer 事务池执行；租户上下文仅在事务内生效，缺失时默认拒绝访问。
- **凭证**：`EnvelopeCryptoService` 使用每租户 DEK 和 AES-256-GCM 信封加密，AAD 绑定租户、账户及凭证类型，覆盖 OAuth Token、邮件凭证和 BYOK 密钥。
- **业财与物流**：金额、汇率、成本与 CVP 计算使用 Python `Decimal`（`ROUND_HALF_UP`）和 PostgreSQL `NUMERIC`；17TRACK 自动查询固定 `cacheLevel=0`。
- **AI 治理**：实时价格、MOQ、库存、订单和物流只用 `ErpToolRegistry` 的确定性工具取数，通过 `source_tool_call_id` 验证来源；严格检索无可靠依据时拒答。L0 仅辅助、L1 提案经人工审核、L2 仅按策略自动执行。
- **服务边界**：Next.js 只做 UI、鉴权代理和 SSE 转发，不写业务 SQL、不直连数据库；跨后端模块边界由 [`infra/importlinter.ini`](infra/importlinter.ini) 检查。

## 本地启动

要求：Python 3.12+ 与 `uv`、Node.js 22+ 与 pnpm 9、Docker Compose v2（仅全栈和后端集成测试需要）。除另有说明，命令从仓库根目录运行。

### 全栈开发

使用独立的本地开发环境；示例凭证不可用于生产。Compose 启动 PG17/pgvector（`5432`）、PgBouncer（`6432`）、Redis（`6379`）以及 S3 兼容的 RustFS（Compose 服务名 `minio`，`9000`/`9001`）。

```bash
test -f .env || cp .env.example .env
set -a; . ./.env; set +a
uv sync
pnpm install --frozen-lockfile
docker compose -f infra/docker-compose.yml up -d --wait
uv run --directory apps/backend alembic upgrade head
uv run python apps/backend/scripts/seed_hybrid_factory.py
```

从仓库根目录在独立终端分别运行下列进程；如使用自定义 `.env`，相应终端也应先导出其变量。

| 进程 | 启动命令 | 地址 / 用途 |
| --- | --- | --- |
| FastAPI | `uv run uvicorn erp_core.api_main:app --host 127.0.0.1 --port 8000` | `http://localhost:8000`，健康 `/healthz`、就绪 `/readyz` |
| Taskiq Worker | `uv run taskiq worker erp_core.worker_main:broker` | 消费 Taskiq 队列 |
| Webhook Stream Worker | `uv run python -m erp_core.worker_main` | 消费原始 webhook 的 Redis Streams |
| Next.js | `NEXT_PUBLIC_USE_MOCK_API=false pnpm --filter web dev` | `http://localhost:3000`；`BFF_BACKEND_URL` 默认 `http://localhost:8000` |
| Widget | `pnpm --filter widget dev` | Vite 开发服务端口 `3001`；构建产物见下方 |

演示账号：租户 `quanzhou-led-factory`，`owner@quanzhou-led.example.com`，密码 `FactoryPass2026!`（仅本地种子数据）。种子脚本初始化租户、账号、示例凭证和入驻状态，不提供完整的浏览器场景数据。

### 不启动后端的前端开发

```bash
pnpm install --frozen-lockfile
NEXT_PUBLIC_USE_MOCK_API=true pnpm --filter web dev
```

Mock 数据与 BFF 响应用于前端开发，不验证真实鉴权、持久化或第三方通道。运行下方 mock E2E 前请停止这个开发服务，让 Playwright 自行启动并占用 `3000`。

## 常用测试与质量门禁

**后端**（集成 pytest 需要上述服务与迁移；使用可用于测试的数据库）：

```bash
uv run ruff check apps/backend
uv run mypy apps/backend/src
uv run lint-imports --config infra/importlinter.ini
uv run pytest -v
uv run python apps/backend/scripts/export_openapi.py
git diff --exit-code contracts/openapi.json
```

**Web / Widget**：在根目录执行 `pnpm install --frozen-lockfile` 后运行：

```bash
pnpm --filter web lint       # ESLint：禁止数据库驱动 / ORM 等导入
pnpm --filter web typecheck
pnpm --filter web test       # Vitest 全量
pnpm --filter web build
pnpm --filter widget build

# Bash；严格小于 35 KiB（35,840 字节）
(set -o pipefail
 bytes=$(gzip -c apps/widget/dist/livechat-widget.iife.js | wc -c) || exit 1
 printf 'Widget gzip: %d bytes\n' "$bytes"
 test "$bytes" -lt 35840)
```

**Chromium E2E**：在根目录执行 `pnpm --filter e2e exec playwright install chromium`（Linux CI 加 `--with-deps`）。

```bash
E2E_MODE=mock pnpm --filter e2e test
E2E_MODE=live pnpm --filter e2e test
```

- `mock`（默认、PR 门禁）：自动在 `http://localhost:3000` 启动 Next.js，显式开启 mock BFF，不接触 Docker 或数据库；覆盖真实 Chromium 的交互和渲染，不等同于全栈验收。
- `live`（联调入口，非当前 PR 门禁）：不代启服务。需要预先运行关闭 mock 的 Web 和已配置的后端，可用 `E2E_BASE_URL` 指向 Web（默认 `http://localhost:3000`），`E2E_API_URL` 指向 webhook 目标（默认 `http://localhost:8000`）。需对齐两端 webhook 签名密钥、设置 Web 的 `NEXT_PUBLIC_DEFAULT_WAREHOUSE_ID`，并准备各场景的会话、报价、待核销收款、CVP 与可入驻工作区。当前种子脚本本身不足以支撑这三条流程；还需补齐测试依赖的 AI/计量、入驻及邮件入站接口。只对可写入的专用联调环境运行。

[CI Gate](.github/workflows/ci.yml) 并行运行后端与 OpenAPI 契约、Web/Widget 和 mock E2E。暂未启用前端 `openapi-ts` 漂移检查。

## Day-1 合规与工程索引

[合规材料目录](docs/compliance/)中的文档用于送审准备，不代表已获平台批准：

- [Meta Tech Provider 与 Embedded Signup](docs/compliance/01-meta-tech-provider-submission.md)
- [Shopify Protected Customer Data Level 2 与 GDPR](docs/compliance/02-shopify-pcd-level2-and-gdpr.md)
- [TikTok Shop DSPR 与 Amazon SP-API DPP](docs/compliance/03-tiktok-dspr-and-amazon-dpp.md)
- [Hybrid Factory 演示知识库种子包](docs/compliance/04-hybrid-factory-kb-seed-pack.md)

工程文档见 [架构总规格](docs/superpowers/specs/2026-11-18-omnichannel-erp-deep-research-and-architecture.md)、[Master Plan 与共享契约](docs/superpowers/plans/2026-11-19-00-phase1-2-master-plan-index.md)和[分阶段实施计划](docs/superpowers/plans/)。
