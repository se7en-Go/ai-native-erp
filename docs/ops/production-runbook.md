# 生产运维手册 · Production Operations Runbook

> Track 8 — 可观测性、备份/恢复（PITR 演练）与 Stripe 闭环计费。
> Scope: `infra/scripts/*.sh`, `GET /healthz` `/readyz` `/metrics` `/api/v1/ops/health`,
> `apps/backend/src/erp_core/{shared/observability.py,tenancy/ops_router.py,billing/**}`,
> `apps/web` → `/settings/channels?tab=compliance`（运维与合规）。

所有命令都在仓库根目录执行，且只依赖 `docker` / `curl` / `psql` / `bash`。
Every command below runs from the repository root and needs only `docker` / `curl` / `psql` / `bash`.

---

## 1. 每日巡检 / Daily healthcheck

```bash
# 生产机（容器名/mapped 端口按 .env.prod 实际值覆盖）
BACKEND_URL=http://127.0.0.1:8000 \
WEB_URL=http://127.0.0.1:3000 \
PG_CONTAINER=erp_prod_postgres \
REDIS_CONTAINER=erp_prod_redis \
PG_USER=postgres PG_DATABASE=erp_saas \
./infra/scripts/healthcheck.sh
```

检查项 / what it checks: `/healthz`、`/readyz`（含 Postgres+Redis 依赖）、`/metrics`
（含 `http_requests_total`）、前端 `/login`、`pg_isready`、`redis-cli PING`。
任一失败即 `exit 1`，可直接挂 cron / K8s `readinessProbe` / 监控探针：

```cron
*/5 * * * * cd /srv/ai-native-erp && ./infra/scripts/healthcheck.sh >> /var/log/erp/healthcheck.log 2>&1
```

管理台同源视图 / same snapshot in the UI：`/settings/channels` → `运维与合规`（仅 owner/admin）
调用 `GET /api/v1/ops/health`，展示 DB 延迟、Redis 内存、Stream 积压、死信数量与钱包余额。

### 常用逐项排查 / per-surface spot checks

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/readyz | jq .
docker exec erp_prod_postgres pg_isready -U postgres -d erp_saas
docker exec erp_prod_redis redis-cli PING
```

---

## 2. 指标与告警阈值 / Metrics & alert thresholds

`GET /metrics` 输出 Prometheus 文本（`text/plain; version=0.0.4; charset=utf-8`），零外部依赖：

| Metric | Type | Labels | 含义 / Meaning |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`,`path_template`,`status` | 请求数；`path_template` 是路由模板，不是原始 URL（基数有界） |
| `http_request_duration_seconds` | histogram | `method`,`path_template`,`le` | 延迟直方图，桶：0.01/0.05/0.1/0.25/0.5/1/2.5/5/`+Inf` |
| `erp_stream_backlog_messages` | gauge | `stream` | 每次 ops 巡检写入的 Redis Stream 积压深度 |

```yaml
# prometheus.yml 片段 / scrape config
scrape_configs:
  - job_name: erp-backend
    metrics_path: /metrics
    static_configs:
      - targets: ["backend:8000"]        # 内网；/metrics 不鉴权，切勿直接暴露公网
```

建议告警 / suggested rules:

```yaml
- alert: ErpHttp5xx
  expr: sum(rate(http_requests_total{status=~"5.."}[5m])) > 0.2
  for: 10m
- alert: ErpP99Latency
  expr: histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m]))) > 1.5
  for: 10m
- alert: ErpStreamBacklog
  expr: max(erp_stream_backlog_messages) > 1000
  for: 15m
```

阈值来自环境变量（`apps/backend/src/erp_core/shared/config.py`）：

| Env | 默认 / default | 说明 / Notes |
| --- | --- | --- |
| `OPS_QUEUE_WARN_DEPTH` | `100` | `/api/v1/ops/health` 返回 `warn` 的积压深度 |
| `OPS_QUEUE_CRIT_DEPTH` | `1000` | 返回 `critical` 的积压深度（必须 ≥ warn） |
| `SENTRY_DSN` | 空 / empty | 配置后才发送错误事件，否则严格 no-op |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空 / empty | 配置后向 `<endpoint>/v1/traces` 发送 span，否则 no-op |

`infra/scripts/healthcheck.sh` 的 `/metrics` 检查仅验证 exposition 可用；真正的曲线看 Prometheus。

---

## 3. 队列积压处理 / Queue backlog remediation

### 3.1 先看哪个 Stream 积压 / find the hot stream

```bash
curl -s -H "Authorization: Bearer $OWNER_JWT" http://127.0.0.1:8000/api/v1/ops/health | jq '.status, .max_backlog, .alerts, .streams'
```

关注字段 / read these fields: `status`（`ok|warn|critical`）、`alerts[]`（指名最深的 stream）、
`streams[]`（`length`/`pending`/`consumers`）、`postgres.raw_webhook_status_counts`、
`postgres.dead_letter_events`、`postgres.notification_deliveries_pending`、
`postgres.inventory_sync_jobs_pending`。

本部署实际消费的 stream / streams consumed by this deployment:
`stream:webhooks:<channel_type>`、`stream:outbound:messages`、`stream:erp:events`。

```bash
docker exec erp_prod_redis redis-cli --scan --pattern '*stream*' | sort
docker exec erp_prod_redis redis-cli XLEN stream:webhooks:whatsapp_cloud
docker exec erp_prod_redis redis-cli XINFO GROUPS stream:webhooks:whatsapp_cloud
```

### 3.2 判断方向 / decide the remediation

| 现象 / Symptom | 处理 / Action |
| --- | --- |
| `length` 涨、`pending` 稳定 | worker 变慢/停顿：`docker compose -f infra/docker-compose.prod.yml logs -f worker`，先扩容 worker，切勿 `DEL` stream |
| `pending` 涨、`consumers = 0` | 消费者已死：重启 `worker` 容器，让 group 重新认领；必要时 `XAUTOCLAIM` |
| `raw_webhook_status_counts.dead_letter > 0` 或 `postgres.dead_letter_events > 0` | 修复下游后在 `运维与合规` 面板或 API 上重放（幂等键保证不重复入账） |
| `notification_deliveries_pending` 长期 > 0 | 检查飞书/钉钉/企业微信 webhook 目标与 `next_retry_at` |
| `inventory_sync_jobs_pending` 长期 > 0 | 检查对应渠道 token 是否 `token_expired`，修复后下次轮询自动重试 |

重放死信 / replay DLQ（幂等，不会重复入账）：

```bash
curl -s -X POST -H "Authorization: Bearer $OWNER_JWT" \
  http://127.0.0.1:8000/api/v1/channels/dlq/$DLQ_ID/replay | jq .
```

**禁止 / Never**: `redis-cli DEL stream:*`（丢消息，违反零丢消息约束）、
直接 `UPDATE raw_webhook_events`（绕过审计与幂等）。

---

## 4. 备份 / Backup

```bash
sudo install -d -m 750 /var/backups/erp
BACKUP_DIR=/var/backups/erp BACKUP_RETENTION_DAYS=14 \
PG_CONTAINER=erp_prod_postgres PG_USER=postgres PG_DATABASE=erp_saas \
./infra/scripts/backup.sh
```

产物 / artifacts:

- `/var/backups/erp/erp_saas_<UTC>.dump` — `pg_dump -Fc` 自定义格式；
- `...dump.sha256` — 校验和 sidecar；
- 先写 `.partial` 再 `mv`：发布前已用 `pg_restore --list` 校验，截断的 dump 不会被当成可用备份；
- 自动清理 `BACKUP_RETENTION_DAYS`（默认 14）之前的 `.dump` 与 `.sha256`。

```cron
30 2 * * * cd /srv/ai-native-erp && BACKUP_DIR=/var/backups/erp PG_CONTAINER=erp_prod_postgres ./infra/scripts/backup.sh >> /var/log/erp/backup.log 2>&1
```

异地留存 / offsite: 同步到对象存储（R2/S3），至少保留一份跨区域副本；
WAL/PITR 连续性由 `archive_mode=on` + `archive_command` 负责（见 `infra/docker-compose.prod.yml`
的 `postgres` 服务），本脚本只保证**全量逻辑备份可用**。

---

## 5. 恢复 / Restore

```bash
# 默认恢复到非生产库 erp_restore_verify（安全默认）
PG_CONTAINER=erp_prod_postgres \
./infra/scripts/restore.sh /var/backups/erp/erp_saas_20261126T023000Z.dump

# 恢复到指定库
PG_CONTAINER=erp_prod_postgres \
./infra/scripts/restore.sh <dump> --database erp_staging

# 覆盖生产库：必须显式加危险开关
PG_CONTAINER=erp_prod_postgres \
./infra/scripts/restore.sh <dump> --database erp_saas --yes-overwrite-prod
```

强制校验 / hard guards:

1. dump 必须存在、非空，且 SHA-256 与 sidecar（或 `EXPECTED_SHA256`）一致；
2. `PROD_DB`（默认 `erp_saas`）没有 `--yes-overwrite-prod` 时 **拒绝恢复**（exit 3）；
3. `pg_restore --clean --if-exists --no-owner --single-transaction` 在单事务内完成，失败自动回滚；
4. 恢复后校验 public 表数量、`tenants`/`orders` 的 `rowsecurity = true`、`alembic_version` 非空；
5. 需要跑真正的 `alembic current` 时：
   ```bash
   ALEMBIC_CMD="uv run --directory apps/backend alembic current" ./infra/scripts/restore.sh <dump> --database erp_staging
   ```
   （`alembic_version.version_num` 就是 `alembic current` 读取的那一行，脚本默认直接读它。）

被覆盖前先留证据 / before overwriting prod:

```bash
docker exec erp_prod_postgres pg_dump -Fc -U postgres -d erp_saas > /var/backups/erp/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump
```

---

## 6. PITR / 恢复演练 / Restore drill

```bash
PG_CONTAINER=erp_prod_postgres PG_USER=postgres PG_DATABASE=erp_saas \
DRILL_DB=erp_pitr_drill ./infra/scripts/pitr_drill.sh
```

演练做了什么 / what it proves（全程非破坏，`trap` 保证清理）：

1. 在临时目录跑 `backup.sh`（不写生产备份目录）；
2. 创建临时库 `erp_pitr_drill`；
3. 校验并恢复 dump；
4. 比对 public 表数量、`tenants` / `orders` / `order_items` / `products` 行数与源库一致，
   且 `tenants` / `orders` 仍为 `rowsecurity = true`；
5. 删除临时库与临时 dump。

建议节奏 / cadence: 每周一次 + 每次升级/迁移后一次；结果写入值班日志。
目标 RTO：单库恢复到新实例 < 30 分钟；目标 RPO：全量备份 ≤ 24h（叠加 WAL 归档可达分钟级）。

---

## 7. Sentry / OpenTelemetry 开启 / enabling telemetry

默认**完全关闭**：`SENTRY_DSN` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 为空时，
`dispatch_error_event` / `dispatch_trace_span` 立即返回 `False`，不发任何网络请求，
也不影响请求处理（收集器不可达同样被静默吞掉）。

```bash
# .env.prod
SENTRY_DSN=https://<public-key>@o0.ingest.sentry.io/<project-id>
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

```bash
docker compose -f infra/docker-compose.prod.yml --env-file .env.prod up -d backend worker
```

行为 / behaviour:

- 未捕获异常（`ObservabilityMiddleware` 捕获并重抛）→ 每个请求最多 **1** 条 Sentry 事件，
  带 `request_id`、`path`、`method`、`status`、`exception_type`；
- OTLP span → `POST <endpoint>/v1/traces`（OTLP/HTTP + JSON），带 `service.name` 与环境标签；
- 交付在后台任务/线程中进行，超时 2s，`try/except BaseException` 兜底；
- 响应头自带 `X-Request-ID`：上游（Cloudflare/Nginx）传入的合法 id（`[A-Za-z0-9._-]{1,64}`）会被沿用，
  否则生成 UUID4 —— 排查时用 `request_id` 在 Sentry 与访问日志间关联。

关掉 / rollback: 清空这两个变量并滚动重启即可，无需改代码。

---

## 8. Stripe 闭环计费 / Stripe closed-loop billing

Webhook：`POST /api/v1/billing/stripe/webhook`，验签 `Stripe-Signature`（HMAC-SHA256 `v1`，
时间戳 ≤ 300s），失败一律 `401 ERR_INVALID_STRIPE_SIGNATURE`；`403/422` 不会让 Stripe 静默丢事件。

| 事件 / Event | 动作 / Action | 幂等键 / Idempotency |
| --- | --- | --- |
| `payment_intent.succeeded` | 充值到账（`apply_stripe_topup`） | `funding:stripe:<payment_intent id>` |
| `invoice.paid`（`metadata.erp_credit_topup=1`） | 充值到账 | `funding:stripe:<invoice id>` |
| `invoice.payment_failed` | 降级 `starter` + 关闭 overage + 强制 L0 | `billing:event:<invoice id>` |
| `customer.subscription.deleted` | 同上 | `billing:event:<subscription id>` |
| 无 `metadata.tenant_id` | `200 {"status":"ignored"}`（无法归属租户） | — |

```bash
stripe listen --forward-to http://127.0.0.1:8000/api/v1/billing/stripe/webhook
stripe trigger invoice.paid
```

钱包看板：`/settings/channels` → `运维与合规` 中显示 `plan_tier`、可用额度与最近 usage 条目；
用量接口 `GET /api/v1/billing/usage`（只列 AI 消耗，`funding:`/`billing:event:` 为账本注记）。

钱包耗尽 + `auto_downgrade_to_l0=true` 时 AI 调用降级为 L0（不扣费、不自动执行），
充值到账后自动恢复，无需重启。

---

## 9. 事故 checklist / Incident checklist

1. `healthcheck.sh` 拿快照 → 2. `GET /api/v1/ops/health` 判断是 DB、Redis、Stream 还是死信 →
3. 修下游、重放 DLQ（幂等）→ 4. 观察 `http_requests_total{status=~"5.."}` 与
`erp_stream_backlog_messages` 回落 → 5. 若需回滚数据：`restore.sh`（先 `--database` 到非生产库验证）→
6. 事后补一次 `pitr_drill.sh` 并把结论写进值班记录。
