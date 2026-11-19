# Phase 3 Master Plan & Extensible Contract Registry (`ai-native-erp`)

> **Status**: Approved for execution on top of `main@d2ea631` (Phase 1–2 complete, 17 PRs merged, 8-container Docker Compose production stack verified).
> **Applies to**: All Phase 3 & future Phase 4+ extensions (`pi` and human contributors).
> **Design Principle**: Modular, zero-regression vertical slices built on the existing Phase 1–2 non-negotiable invariants (`FORCE RLS`, PgBouncer transaction pooling, AES-256-GCM `EnvelopeCryptoService`, `Decimal` financial precision, zero SQL in `apps/web`, `@hey-api/openapi-ts` CI drift gate, strict TDD).

---

## 1. Extensibility Architecture (Built for Future Phase 4+ Expansion)

To guarantee that adding Phase 3 (A/B/C/D) and future Phase 4+ modules never requires refactoring the core shell or breaking module boundaries:

1. **Frontend Navigation & Command Registry (`apps/web/src/lib/nav-registry.ts`)**:
   - Single typed registry defining collapsible sidebar sections (`核心工作台`, `业务核心`, `交易与供应链`, `财务中心`, `AI 赋能`, `获客与社媒`, `系统工具`), route metadata, RBAC role visibility (`canAccessRoute`), i18n keys, live badge counters, and `Ctrl+K` global command palette entries.
   - Adding any future vertical requires adding 1 route group under `apps/web/src/app/<module>/` + 1 descriptor entry in `nav-registry.ts`.
2. **Backend Modular Monolith Packages (`apps/backend/src/erp_core/`)**:
   - Preserve existing `infra/importlinter.ini` 6 contracts and add matching isolation contracts for new packages:
     - `erp_core.supply_chain` (Module 3A: suppliers, purchase orders, WMS movements, executive analytics)
     - `erp_core.growth` (Module 3C: compliant global map lead collection, prospect cleaning/grading, outbound campaigns)
     - `erp_core.social` (Module 3D: multi-platform social matrix, content calendar, AI copy/asset studio, mentions/listening)
   - Extend existing packages strictly inside their own boundaries:
     - `erp_core.crm`: standalone leads, opportunities Kanban, customer follow-ups, task reminders
     - `erp_core.ai_engine`: AI digital employee personas, 7-day metrics, L0/L1/L2 division matrix, inbox AI quick actions (polish / summarize / suggest / daily brief), quick-reply templates, creative asset studio
     - `erp_core.channels`: Messenger, Instagram DM, Telegram Business adapters
3. **Linear Alembic Migration Chain (<= 32-char `revision` IDs, strictly reversible `downgrade()`)**:
   - `0004` (merged) -> `0005_phase3a_crm_supply_wms` (27 chars) -> `0006_phase3b_ai_employees` (25 chars) -> `0007_phase3c_growth_leads` (25 chars) -> `0008_phase3d_social_matrix` (26 chars).
   - Every tenant table MUST enforce `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + `RESTRICTIVE` `tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid` + `GRANT SELECT, INSERT, UPDATE, DELETE TO app_runtime`.
4. **Full-Stack Vertical Delivery Rule**:
   - Because `.github/workflows/ci.yml` enforces both `export_openapi.py --check` and `pnpm --filter web openapi-ts` zero-drift, every Phase 3 vertical PR ships **end-to-end**: Migration + Backend Service/Router/Tests + `contracts/openapi.json` + regenerated `apps/web/src/lib/api-client/` + Next.js Pages/Components + Vitest + demo seed data (`seed_hybrid_factory.py --with-demo-data`), keeping all 3 CI jobs (`backend-and-contracts`, `frontend-and-widget`, `e2e-mock`) 100% green.

---

## 2. Vertical 3A (`PR #18`): Executive Dashboard, Extensible Sidebar + `Ctrl+K`, Standalone CRM, Supply Chain/PO, WMS & Analytics

- **Migration**: `apps/backend/alembic/versions/0005_phase3a_crm_supply_wms.py` (`revision = "0005_phase3a_crm_supply_wms"`, `down_revision = "0004"`).
- **New Tables (all `FORCE RLS`)**:
  - `customer_followups` (`id`, `tenant_id`, `customer_id`, `opportunity_id`, `author_user_id`, `followup_type`, `summary`, `next_action_at`, `created_at`)
  - `suppliers` (`id`, `tenant_id`, `code`, `name`, `contact_name`, `contact_email`, `contact_phone`, `country`, `lead_time_days`, `payment_terms`, `rating`, `status`, `created_at`, `updated_at`)
  - `purchase_orders` (`id`, `tenant_id`, `po_no`, `supplier_id`, `warehouse_id`, `currency`, `total_amount NUMERIC(14,2)`, `status`, `expected_arrival_date`, `notes`, `created_by`, `created_at`, `updated_at`)
  - `purchase_order_items` (`id`, `tenant_id`, `purchase_order_id`, `variant_id`, `sku`, `qty_ordered`, `qty_received`, `unit_cost NUMERIC(14,4)`)
  - `wms_movements` (`id`, `tenant_id`, `movement_no`, `warehouse_id`, `variant_id`, `movement_type` in `inbound_po|outbound_order|transfer|stocktake_adjust`, `qty_delta`, `reference_type`, `reference_id`, `operator_user_id`, `notes`, `created_at`)
  - `task_reminders` (`id`, `tenant_id`, `title`, `due_at`, `priority`, `status`, `linked_entity_type`, `linked_entity_id`, `assignee_user_id`, `created_at`)
- **Backend APIs (`/api/v1/*`)**:
  - `GET /api/v1/analytics/dashboard` (role-aware KPIs: open inquiries, B2B pipeline value, pending proposals, B2C orders, low-stock alerts, CVP net margin — masks cost/margin for `agent`)
  - `GET /api/v1/analytics/overview` (channel mix, B2B funnel conversion, SKU margin breakdown)
  - `GET /api/v1/search?q=...` (tenant-scoped global search across customers, orders, quotes, SKUs for `Ctrl+K`)
  - `GET/POST/PATCH /api/v1/b2b/leads`, `GET/POST/PATCH /api/v1/b2b/opportunities`, `GET/POST /api/v1/b2b/followups`, `GET/POST/PATCH /api/v1/b2b/tasks`
  - `GET/POST/PATCH /api/v1/supply-chain/suppliers`
  - `GET/POST/PATCH /api/v1/supply-chain/purchase-orders`, `POST /api/v1/supply-chain/purchase-orders/{po_id}/receive` (atomically updates `qty_received`, writes `wms_movements`, and increments `inventory_balances` via `InventoryLedgerService`)
  - `GET/POST /api/v1/wms/movements`, `GET /api/v1/wms/stock`
- **Frontend Pages & Shell Upgrade**:
  - Upgrade `AppShell` to use `apps/web/src/lib/nav-registry.ts` (Nova-style collapsible grouped sidebar + top bar with `Ctrl+K` global search modal, locale switcher, quick-create `+` menu, pending notifications bell, user menu).
  - Pages: `/dashboard` (executive home), `/crm/leads`, `/crm/customers`, `/crm/opportunities` (stage Kanban board), `/crm/followups`, `/supply-chain/suppliers`, `/supply-chain/purchase-orders`, `/wms`, `/analytics`, `/tasks`.
  - Keep existing `/inbox`, `/b2b/quotes`, `/catalog`, `/orders`, `/finance`, `/ai/proposals`, `/ai/capabilities`, `/settings/channels`, `/onboarding` routes 100% backward-compatible for all existing E2E & Vitest tests!

---

## 3. Vertical 3B (`PR #19`): AI Digital Employees Center, L0/L1/L2 Division Matrix, Creative Studio & Inbox AI Toolbar

- **Migration**: `apps/backend/alembic/versions/0006_phase3b_ai_employees.py` (`revision = "0006_phase3b_ai_employees"`, `down_revision = "0005_phase3a_crm_supply_wms"`).
- **New Tables (all `FORCE RLS`)**:
  - `ai_employees` (`id`, `tenant_id`, `code`, `display_name`, `role_title`, `department`, `avatar_emoji`, `description`, `capability_codes JSONB`, `autonomy_level`, `is_enabled`, `tool_count`, `system_persona`, `created_at`, `updated_at`) — seeded with realistic factory digital employees mapped strictly onto real `AiCapability` + `ErpToolRegistry` tools (B2B 外贸业务员、B端跟单专员、物流出船专员、获客专员、B2C 贴心客服、财务助理、供应链专员、经营分析师、文案与社媒专员 — zero fake mock metrics; 7-day action/task counts & daily USD LLM cost computed from real `usage_ledger` & `ai_proposals`!).
  - `quick_reply_templates` (`id`, `tenant_id`, `title`, `category`, `language`, `body_text`, `shortcut_code`, `created_at`)
  - `ai_creative_assets` (`id`, `tenant_id`, `asset_type` in `product_copy|listing|email_template|social_caption|ad_hook`, `title`, `language`, `sku_ref`, `prompt`, `content_text`, `status`, `created_by`, `created_at`)
- **Backend APIs (`/api/v1/ai/*`)**:
  - `GET/POST/PATCH /api/v1/ai/employees` + `GET /api/v1/ai/employees/matrix` (returns real L2 auto-executed counts, L1 pending proposals grouped by type, L0 locked high-risk guardrails, and today's `Decimal` LLM USD cost from `usage_ledger`)
  - `POST /api/v1/ai/inbox/assist` (actions: `polish`, `summarize`, `suggest_reply`, `daily_brief` — runs through `TieredModelRouter` + `CreditWalletService` + `ToolGroundedNumericGuard`)
  - `GET/POST/DELETE /api/v1/ai/quick-replies`
  - `GET/POST/PATCH /api/v1/ai/studio/assets`
- **Frontend Pages & Enhancements**:
  - `/ai/employees`: Nova-inspired AI Digital Employee management center (top KPI cards: total employees / active / 7-day actions / today's LLM USD cost / autonomy badge; employee card grid with enable toggle & detail drawer; bottom 3-column **L2 全接管 / L1 待人审 / L0 人工把关** live governance matrix).
  - `/ai/studio`: AI copy & listing asset workbench (tied to real Catalog SKUs + `ToolGroundedNumericGuard`).
  - Upgrade `/inbox` composer toolbar with **AI 客服 / AI 润色 / 消息总结 / AI 建议 / 快捷短语** action buttons, and upgrade `FloatingCopilot` with quick-action chips (`AI 日报`, `待审提案`, `今日跟进`, `库存预警`) — strictly zero `[object Object]` or raw key leaks!

---

## 4. Vertical 3C (`PR #20`): Compliant Global Map Lead Collection, Prospect Cleaning Pool & Outbound Campaigns

- **Compliance Guardrail (Non-Negotiable)**:
  - **NEVER** build unauthorized scrapers that violate Meta/Facebook/WhatsApp ToS or risk account bans.
  - Use **compliant primary sources** documented in `docs/research/04-compliant-lead-generation.md`:
    1. **Google Places API (New)** (when tenant configures `GOOGLE_PLACES_API_KEY` via BYOK/env),
    2. **OpenStreetMap Overpass / Overture Maps POI** (open-license fallback, no scraping ToS risk, injectable `httpx.MockTransport` for deterministic tests),
    3. **CSV / Trade Show / B2B Directory Import**.
  - All outbound email campaigns MUST enforce RFC 8058 `List-Unsubscribe` + `ConsentAndSuppressionService` suppression checks; WhatsApp campaigns MUST enforce opt-in + US marketing prohibition + dual token bucket rate limits.
- **Migration**: `apps/backend/alembic/versions/0007_phase3c_growth_leads.py` (`revision = "0007_phase3c_growth_leads"`, `down_revision = "0006_phase3b_ai_employees"`).
- **New Tables (all `FORCE RLS`)**:
  - `lead_collection_jobs` (`id`, `tenant_id`, `source_provider` in `google_places|overpass_osm|overture_poi|csv_import`, `keywords JSONB`, `target_region`, `max_results`, `min_rating NUMERIC(3,2)`, `require_phone`, `status`, `collected_count`, `cleaned_count`, `synced_count`, `error_message`, `created_by`, `created_at`, `completed_at`)
  - `collected_prospects` (`id`, `tenant_id`, `job_id`, `company_name`, `contact_name`, `phone_e164`, `email`, `website`, `country`, `city`, `latitude DOUBLE PRECISION`, `longitude DOUBLE PRECISION`, `rating NUMERIC(3,2)`, `grade` in `A|B|C`, `whatsapp_reachable BOOLEAN`, `dedupe_fingerprint`, `sync_status` in `raw|cleaned|synced_to_crm|discarded`, `crm_lead_id`, `raw_metadata JSONB`, `created_at`, UNIQUE `(tenant_id, dedupe_fingerprint)`)
  - `marketing_campaigns` (`id`, `tenant_id`, `name`, `channel_type` in `email|whatsapp_cloud`, `subject`, `body_template`, `status` in `draft|scheduled|running|paused|completed`, `total_targets`, `sent_count`, `suppressed_count`, `replied_count`, `created_by`, `created_at`)
  - `campaign_recipients` (`id`, `tenant_id`, `campaign_id`, `customer_id`, `prospect_id`, `address`, `status` in `queued|sent|suppressed|failed|replied`, `error_reason`, `sent_at`)
- **Backend APIs (`/api/v1/growth/*`)**:
  - `GET/POST /api/v1/growth/jobs`, `POST /api/v1/growth/jobs/{job_id}/run`
  - `GET /api/v1/growth/prospects`, `POST /api/v1/growth/prospects/clean` (dedup + A/B/C scoring), `POST /api/v1/growth/prospects/sync-crm` (creates real `customers` + `b2b_companies` + `leads` inside `tenant_session`), `GET /api/v1/growth/prospects/export.csv`
  - `GET/POST/PATCH /api/v1/growth/campaigns`, `POST /api/v1/growth/campaigns/{id}/launch` (strictly runs `ConsentAndSuppressionService` & compliance gates before queuing)
- **Frontend Pages**:
  - `/growth/collect`: Interactive global map (`leaflet` / `react-leaflet` dynamically imported client-side with zero SSR window crash!) showing pinpointed prospects, left-hand job config panel (keywords, city/region, source selector, min rating, require phone), KPI summary cards (collected / cleaned / synced / jobs), bottom prospect cleaning table with Grade A/B/C badges, CSV export, and one-click `同步到 ERP 线索池`.
  - `/growth/campaigns`: Outbound email & compliant WA opt-in campaign builder with live suppression preview.

---

## 5. Vertical 3D (`PR #21`): Social Media Marketing Center & Messaging Channel Expansion (Messenger / IG DM / Telegram)

- **Migration**: `apps/backend/alembic/versions/0008_phase3d_social_matrix.py` (`revision = "0008_phase3d_social_matrix"`, `down_revision = "0007_phase3c_growth_leads"`).
- **New Tables (all `FORCE RLS`)**:
  - `social_accounts` (`id`, `tenant_id`, `platform` in `instagram|tiktok|facebook|youtube|linkedin|pinterest|x|xiaohongshu|douyin|kuaishou|bilibili|wechat_video|wechat_oa|weibo`, `region` in `overseas|domestic`, `handle`, `display_name`, `external_account_id`, `status` in `connected|disconnected|token_expired`, `followers_count`, `config JSONB`, `created_at`, `updated_at`)
  - `social_posts` (`id`, `tenant_id`, `social_account_id`, `platform`, `title`, `caption`, `hashtags JSONB`, `media_urls JSONB`, `sku_ref`, `status` in `draft|scheduled|published|failed`, `scheduled_at`, `published_at`, `external_post_id`, `metrics JSONB`, `created_by`, `created_at`, `updated_at`)
  - `social_mentions` (`id`, `tenant_id`, `social_account_id`, `platform`, `author_handle`, `content_text`, `sentiment` in `positive|neutral|negative|lead_intent`, `converted_lead_id`, `occurred_at`, `created_at`)
- **Backend APIs**:
  - `/api/v1/social/*`: platform matrix playbook catalog (14 overseas + domestic platforms with audience/best-practice metadata), account connect/disconnect (OAuth tokens encrypted via `EnvelopeCryptoService`), content calendar CRUD + publish adapter, AI caption generator (grounded in Catalog SKUs via `TieredModelRouter`), social mentions & 1-click convert-to-lead.
  - **Messaging Channel Expansion (`erp_core.channels.adapters`)**:
    - `messenger.py` (Meta Messenger Platform with `HUMAN_AGENT` 7-day tag + HMAC-SHA256 verification)
    - `instagram_dm.py` (Instagram Messaging API with `HUMAN_AGENT` + story mention handling)
    - `telegram.py` (Telegram Bot / Business Mode webhook `X-Telegram-Bot-Api-Secret-Token` verification + outbound send)
    - Note: `messenger`, `instagram_dm`, `telegram` are **ALREADY** in `0002`'s `_CHANNEL_TYPES` allowlist — zero changes needed to `0002`!
- **Frontend Pages**:
  - `/social`: Tabbed Social Marketing Center matching the reference workflow (`发布中心 & 内容日历`, `平台矩阵 [国内/海外筛选 + 玩法卡片 + 授权状态]`, `贴文编辑器 & AI 文案工坊`, `素材库`, `舆情监听 & 社媒转线索`, `数据看板`).
  - `/settings/channels`: Add Messenger, Instagram DM, Telegram Business connection cards alongside WhatsApp, Email, WebChat, Shopify, WooCommerce, SHOPLINE, Shoplazza, TikTok Shop.

---

## 6. Execution Order & Verification Contract

| PR | Vertical | Alembic Head | Verification |
|---|---|---|---|
| **PR #18** | **Phase 3A** (Dashboard + Nav Registry + `Ctrl+K` + CRM + Supply Chain/PO + WMS + Analytics) | `0005_phase3a_crm_supply_wms` | Backend pytest + Ruff + Mypy + import-linter + OpenAPI drift + Web lint/typecheck/Vitest/build + Widget `<35KB` + CI 3/3 green |
| **PR #19** | **Phase 3B** (AI Employees Matrix + L2/L1/L0 Governance View + Creative Studio + Inbox AI Toolbar) | `0006_phase3b_ai_employees` | Same full gate + 100-case eval regression check |
| **PR #20** | **Phase 3C** (Global Map Lead Collection + Cleaning Pool + Compliant Campaigns) | `0007_phase3c_growth_leads` | Same full gate + zero SSR crash on Leaflet map |
| **PR #21** | **Phase 3D** (Social Marketing Center + Messenger/IG/Telegram Channels) | `0008_phase3d_social_matrix` | Same full gate + local Docker rebuild smoke check (`docker compose -f infra/docker-compose.prod.yml --env-file .env.prod up -d --build`) |
