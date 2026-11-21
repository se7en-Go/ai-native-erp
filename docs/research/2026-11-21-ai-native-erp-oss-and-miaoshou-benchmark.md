# AI-Native 外贸全渠道 ERP 深度调研与架构差距分析报告

> 调研日期：2026-11-21  
> 调研对象：GitHub 头部开源 ERP/AI-CRM 项目（`frappe/erpnext`、`odoo/odoo`、`crbnos/carbon`、`inventree/InvenTree`、`melgarafael/DeskcommCRM`、`Dolibarr/dolibarr`）、国内主流跨境 ERP（**妙手 ERP** / **领星**）、以及截图参考的 **Nova ERP 获客与社媒模块**。

---

## 一、截图对照分析（Nova ERP 获客采集 & 社媒营销中心）

### 1. 截图 1：海外精准线索采集侧边栏对照

| 模块项 | 本系统现状 | 合规与工程结论 |
| --- | --- | --- |
| Google Maps LBS 采集 + 批量多国采集 | **已具备**（`/growth/collect` 免 Key OSM Overpass + 可选 Google Places 多城市批量采集 + 官网公开邮箱提取） | 合规公开数据，继续保留并增强 |
| 清洗规则管理 | **已具备基础去重/A-B-C 评级** | 可持续扩展自定义清洗规则 |
| FB 主页 / 群组 / 视频 / 点赞 / 评论用户采集 | **刻意屏蔽（合规红线）** | 未经授权抓取 Facebook 私域用户数据违反 Meta 开发者条款与 GDPR，极易封号和招致法律风险；合规替代方案为 **Meta Lead Ads 官方 Webhook + 公开主页评论监听** |
| WA 号码批量校验 / WA 消息调度 / FB 私信调度 | **合规替代已落地** | 撞库校验 WhatsApp 号码与未授权冷启动群发会导致 WABA 永久封禁；本系统严格执行 **官方 WhatsApp Cloud API + 客户显式 Opt-in + 零延迟 `STOP` 退订抑制** |

### 2. 截图 2：社媒营销中心对照

| 子模块 | 本系统现状 | 差距与本轮补强 |
| --- | --- | --- |
| 平台矩阵（玩法/受众/漏斗/合规） | **已有 14 平台（海外 7 + 国内 7）** | 已覆盖核心外贸社媒 |
| 发布中心 / 内容日历 / 贴文编辑器 / AI 文案工坊 | **已有**（`/social` + `/ai/studio`） | 已有完整草稿→排期→发布闭环 |
| 舆情监听 & 转线索 | **已有**（评论/提及转 CRM 线索） | 已有 |
| 获客链路归因漏斗 & 数据看板 | **此前偏弱** | **本轮新增 `GET /api/v1/social/attribution-funnel` 与「获客链路看板」**：实时打通 `已发布内容 → 社媒捕获线索 → 转化 CRM 客户 → 关联报价单` |

---

## 二、GitHub 开源 ERP & AI-Native 最佳实践对标

| 开源项目 | 架构亮点 | 相对我们的优势（差距项） | 我们的差异化优势 |
| --- | --- | --- | --- |
| **`frappe/erpnext`** | Python + MariaDB/PG + Frappe 元框架；完整复式总账（GL）、应收/应付（AR/AP）、BOM/MRP 生产、批次序列号、多币种结账 | 1) 完整复式会计分录与资产负债/损益表；2) 应付账款（AP）与采购付款核销；3) 多级 BOM 展开与 MRP 生产排程 | 传统 ERP 是“表单驱动 + 外挂聊天框”，缺乏多渠道外贸聚合沟通、17TRACK、以及 SQL 强制溯源的 AI-Native 工作流 |
| **`crbnos/carbon`** | 2024–2026 新一代开源制造 ERP/MES/QMS，**单一 PostgreSQL Schema + REST API + 原生 MCP Server** | 原生 MCP Server 暴露 ERP 工具给外部 Agent；车间工单/BOM/质检追溯 | 我们已有 `ErpToolRegistry` 与 `FORCE RLS` 多租户硬隔离，下一步可将 `ErpToolRegistry` 直接封装为标准 MCP Server |
| **`inventree/InvenTree`** | Python/Django + React；专精物料 BOM、批次/序列号、多库位追踪、供应商料号映射 | 库位级（Bin）与批次/序列号（Lot/Serial）追踪、物料缺料自动算量 | 我们已实现多仓库、安全库存预警与一键转采购单 PO，后续按需增加 BOM/批次表 |
| **`melgarafael/DeskcommCRM`** | 2026 开源 AI Sales OS：聊天驱动 CRM + 原生 AI 客服代理 + WhatsApp + MCP | **对话驱动的自动回复 + 聊天内容实时反写 CRM 客户画像** | **本轮立即落地！** 结合本系统已有的 Hybrid RAG 知识库 + `ErpToolRegistry` 实时库存/阶梯价 + `L2/L1/L0` 安全护栏，超越纯聊天 CRM |

### AI-Native ERP 核心架构最佳实践（2025–2026 共识）
1. **Event-Driven Customer Graph（事件驱动客户画像）**：不再靠人工手工填 CRM 表单，而是每次买家对话自动提炼意图分、目标国家、感兴趣 SKU、预估采购额、付款/贸易条款偏好与下一步动作，实时写入 `customers.ai_profile` 并联动线索/跟进记录。
2. **Hybrid RAG + Deterministic SQL Grounding（混合检索 + 确定性 SQL 查库）**：知识库文档走 `pgvector + BM25 + RRF`；**库存、MOQ 阶梯价、交期、订单物流 100% 走确定性 SQL 工具**，严禁靠大模型记忆报价。
3. **Confidence + Red-Line Guardrail（分级自治与红线降级）**：
   - **L2 自动回复**：安全咨询（库存、标准阶梯价、交期、物流节点、知识库 FAQ）可直接自动发出并附带证据 ID；
   - **L1 提案人审**：涉及特殊折扣改价、退款赔付、合同承诺或置信度不足时，自动降级为 L1 人工审核提案；
   - **L0 严禁 AI 触碰**：财务收付款核销、出库放行永远人工把关。

---

## 三、妙手 ERP 核心闭环对标与本轮落地

妙手 ERP 在中国跨境卖家中高口碑的核心是 **「货源采集 → 公用采集箱清洗（AI 改写/翻译/定价）→ 多平台一键刊登 → 订单库存联动」**：

1. **缺失的高频跨境渠道（本轮补齐 11 → 14 渠道）**：
   - **Alibaba.com 国际站**（`alibaba_email`：B2B RFQ 询盘与国际站店铺对接）
   - **Amazon 卖家中心**（`amazon_sp`：SP-API 订单、FBA 库存与刊登）
   - **eBay**（`ebay`：eBay Inventory / Fulfillment 店铺对接与刊登）
2. **1688 / URL 商品采集搬家与多平台一键刊登（本轮在 `/catalog` 落地）**：
   - 支持粘贴 1688 商品链接 / 公开供应商 URL 或直接粘贴中文规格文案（严格 SSRF 防护，不绕过反爬验证码，100% 合规）；
   - 按实时 `USD/CNY` 汇率 + 目标毛利率用 `Decimal` 自动算 FOB 零售价与 MOQ 100/500 阶梯价，自动生成英文 SEO 标题与 5 条卖点存入 `sourcing_drafts`（`FORCE RLS`）；
   - 一键导入 ERP 主档 SKU，并一键多平台刊登（自动生成各平台 `channel_sku_mappings`、按平台加价系数换算并记录完整刊登日志）。

---

## 四、ERP 完整性演进路线图

| 阶段 | 核心模块 | 状态 |
| --- | --- | --- |
| **本轮立即交付（PR #35）** | 1) AI 客服自动回复（知识库 + 实时库存/阶梯价 + 红线降级 L1）；2) 聊天自动提炼 CRM 客户画像并同步线索/跟进；3) 渠道扩展至 14 渠道（+ Alibaba 国际站 / Amazon / eBay）；4) 妙手式 1688/URL 采集搬家与多平台一键刊登；5) 财务应付账款（AP）与现金流收支闭环；6) 社媒获客归因漏斗 | **本轮实现并部署** |
| **Phase 5（制造与财务深化）** | 1) 复式总账凭证（Chart of Accounts + Journal Entries + 损益表/资产负债表）；2) 多级 BOM 物料清单与简易 MRP 算料；3) 库位（Bin）与批次/序列号追溯 | 下一迭代规划 |
| **Phase 6（MCP 生态开放）** | 将 `ErpToolRegistry` 封装为标准租户隔离 MCP Server，允许外部 AI Agent/IDE 安全接入 ERP | 下一迭代规划 |
