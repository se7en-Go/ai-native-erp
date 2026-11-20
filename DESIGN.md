# ai-native-erp · 企业级外贸 ERP 设计规范（Stage 1）

> 适用对象：中国跨境外贸企业、工贸一体工厂的业务团队（老板 / 外贸业务员 / 跟单采购 / 仓库 / 财务）。
> 设计目标：**专业、克制、高信息密度**。对标领星 ERP 的数据密度、飞书企业工作台的秩序感、Linear 的极简线条；
> 明确拒绝"AI 味"：不用渐变、不用霓虹发光、不用粗描边大圆角、不用居中巨幅留白。

---

## 1. 视觉基调与色彩层次

| 层级 | 角色 | 色值 / 类名 | 说明 |
| --- | --- | --- | --- |
| 导航层 | 浅灰高对比侧边栏 | `#F8FAFC` (`bg-[#F8FAFC]`) + `border-slate-300` | 柔和护眼，与右侧工作区自然衔接；文字 `text-slate-800` / 图标 `text-slate-600`（对比度 > 10:1） |
| 导航激活 | 品牌蓝 | `#2563EB` | 左侧 3px 竖条 + `bg-blue-50 border-blue-200 text-blue-700 font-semibold` |
| 画布层 | 柔和蓝灰工作画布 | `#E8EEF5` (`bg-[#E8EEF5]`) | 与纯白卡片拉开明确明度差，告别惨白刺眼 |
| 卡片层 | 纯白高对比卡片 | `bg-white border border-slate-300` | 阴影 `0 1px 3px rgba(15,23,42,0.08)`，表头/卡头 `#F8FAFC`/`#F1F5F9` |
| 分割线 | 清晰中性线 | `border-slate-300` / `border-slate-200` | 轮廓一眼可读 |
| 主文字 | 深黑蓝灰 | `text-slate-950` (`#020617`) | 标题 `font-bold` |
| 次文字 | 深中性灰 | `text-slate-700` / `text-slate-600` | 副标题、KPI 标签、说明一律 ≥ `slate-600`，禁止在白底用 `slate-400` 排正文 |
| 容器宽度 | 全宽流式 | `.erp-page` `width: 100%` | 1440 / 1920 / 2560 / 3440 超宽屏铺满无居中孤岛 |

### 语义状态色（状态标签唯一来源）

| 语义 | 场景 | 色板 |
| --- | --- | --- |
| 成功 / 已结算 / 已发布 | 收款核销、库存充足、批准 | `bg-emerald-50 text-emerald-700 border-emerald-200` |
| 待审核 / 预警 / 部分收款 | 待审提案、70% 尾款未收、静默时段 | `bg-amber-50 text-amber-800 border-amber-200` |
| 阻断 / 逾期 / 失败 | 权限拒绝、DLQ、退订拦截 | `bg-rose-50 text-rose-700 border-rose-200` |
| 进行中 / 信息 | 生产中、运输中、AI 提案 | `bg-blue-50 text-blue-700 border-blue-200` |
| 中性 / 草稿 | 草稿、归档、未知 | `bg-slate-100 text-slate-600 border-slate-200` |

**禁止**：`bg-indigo-50` 大色块铺底、彩虹渐变、纯黑 `#000`、无意义的高饱和霓虹色。

---

## 2. 排版与中文外贸数据规范

### 字体栈（`apps/web/src/app/globals.css`）

```css
font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC",
  "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
```

- 中文正文 `text-[13px]` / `text-sm`，行高 `leading-6`；表格正文 `text-[13px]`。
- 页面标题 `text-xl font-semibold tracking-tight`；卡片标题 `text-sm font-semibold`。
- 不使用 `font-black`、`uppercase tracking-widest` 堆砌英文标签。

### 数字与金额（强制）

- 所有金额、汇率、数量、毛利率、百分比、单据号：`tabular-nums`（等宽数字），并加 `font-feature-settings: "tnum"`。
- 金额右对齐，负数 / 扣减项用 `text-rose-700` 前缀 `-`；净贡献毛利用 `text-emerald-700`。
- 汇率固定 6 位小数展示（`1 USD = 7.120000 CNY`），单价保留合同精度。
- 统一通过 `formatMoney(value, currency)` 输出，禁止在 JSX 里手拼货币符号。

### 文案规范

- **单一语言输出**：`zh` 模式纯中文，`en` 模式纯英文。标题、副标题、卡片、按钮、表头、空状态一律通过 `useI18n().bi("中文", "English")`，**禁止 `中文 / English` 斜杠并列**。
- 保留必要的行业技术码（`b2b_30_deposit`、`quote_draft`、`FOB`、`17TRACK`、`cacheLevel=0`）作为小号 badge 或辅助说明，但不作为主标题。
- 术语统一：询盘 / 报价单 / 形式发票（PI）/ 贸易术语 / 阶梯价 / 起订量（MOQ）/ 水单核销 / 净贡献毛利（CVP）/ 尾款 / 提单 / 报关。

---

## 3. 宽屏自适应布局规范

| 页面类型 | 容器 | 说明 |
| --- | --- | --- |
| 标准工作台（财务、报价、商品、订单、AI、CRM、供应链、统计） | `w-full max-w-[1680px] mx-auto px-5 py-5 lg:px-6` | 1440 / 1920 / 2560 均饱满，禁止 `max-w-5xl` / `max-w-6xl` |
| 多栏分屏（`/inbox`、`/growth/collect`、`/social`、`/ai/studio`） | `w-full px-5 py-5`（或直接 `h-[calc(100vh-4rem)]` 满幅） | 中栏弹性，侧栏固定宽 |
| 登录页 | 左右分栏 `lg:grid-cols-[1.1fr_0.9fr]`，整屏 | 左品牌叙事，右登录卡片 |

### 栅格与密度

- KPI 卡：`grid gap-3 sm:grid-cols-2 xl:grid-cols-4`；宽屏允许 `2xl:grid-cols-5`。
- 看板列：`flex gap-3 overflow-x-auto` 或 `xl:grid-cols-6`，列最小宽 `220px`。
- 表格：容器 `overflow-x-auto`，表头 `bg-slate-50/80 text-[11px] font-medium text-slate-500`，行高 `py-2.5`，hover `bg-slate-50/70`。
- 卡片内边距：`p-4`（紧凑）/ `p-5`（标准）；卡片间距 `gap-4`。
- 侧边栏：展开 `w-64`，收起 `w-16`；顶部栏高 `h-16`，`sticky top-0 backdrop-blur`。

---

## 4. 组件约定

### 顶栏（AppShell header）

`sticky top-0 z-30 h-16 border-b border-slate-200/80 bg-white/80 backdrop-blur`
内容顺序：面包屑（当前页中文标题 + 上级分组）→ 租户工作区标签 → `Ctrl+K` 搜索 → `中文 / EN` 切换 → 角色徽章。

### 侧边栏

深色 `bg-[#0B1220]`；分组标题 `text-[11px] font-medium text-slate-500`；条目 `rounded-lg px-3 py-2 text-[13px]`；
激活：`bg-blue-500/10 text-white` + `before:` 左侧 `w-[2px] bg-blue-500` 竖条；徽标 `bg-rose-500 text-white`。
所有 `data-testid` 必须保留：`sidebar-nav`、`sidebar-collapse-btn`、`nav-section-*`、`sidebar-pending-proposals-badge`。

### 表格

- 表头吸顶可选；数字列右对齐；操作列右对齐。
- 空状态：`rounded-xl border border-dashed border-slate-200 p-8 text-center text-sm text-slate-400`。

### 按钮

| 类型 | 类名 |
| --- | --- |
| 主操作 | `bg-blue-600 text-white hover:bg-blue-700 rounded-lg px-3.5 py-1.5 text-[13px] font-medium` |
| 次操作 | `border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 rounded-lg` |
| 危险 | `bg-rose-600 text-white hover:bg-rose-700` |
| 幽灵 | `text-slate-600 hover:bg-slate-100` |

禁止 `rounded-full` 用于常规按钮（仅用于筛选 chip / 状态标签）。

---

## 5. 可访问性与测试约束

- 所有既有 `data-testid` 保留；E2E 依赖的精确文本（`b2b_inquiry`、`b2c_support`、`web_livechat`、`Translated:`、`Preview (EN):`、`Abstained`、`TikTok Shop`、`LED-COB-24V-5M`、`Hans Weber — `）在默认视图下必须可见。
- 交互控件必须有可读 `aria-label`；表单 label 与 input 通过 `htmlFor` 关联。
- 对比度：正文 `slate-900` on white ≥ 12:1；侧边栏文字 `slate-300` on `#0B1220` ≥ 7:1。

---

## 6. 地图（Leaflet）规范

- `TileLayer`：`noWrap: true`，`maxNativeZoom: 19`，OSM 只请求有效 XYZ。
- `Map`：`minZoom: 2`、`maxBounds: [[-85, -180], [85, 180]]`、`maxBoundsViscosity: 1.0`、`worldCopyJump: false`。
- 目标：彻底消除宽屏低缩放下的 `1/-1/0.png` / `1/2/0.png` 越界瓦片 400。
