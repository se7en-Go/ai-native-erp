# AI-Native ERP 多源浏览器一键采集插件 (`apps/collector-extension`)

支持从 **1688（含 `window.__INIT_DATA` 多规格 / 阶梯价 / 原图升级）**、**淘宝 / 天猫**、**拼多多**、**Alibaba 国际站** 以及任意带 OpenGraph / JSON-LD 的商品详情页，一键采集主图、规格变体与成本价推送至 AI-Native ERP 选品箱。

## 1. 加载插件（Chrome / Edge / Ego Lite）

1. 打开浏览器扩展管理页：`chrome://extensions`
2. 打开右上角 **开发者模式（Developer mode）**
3. 点击 **加载已解压的扩展程序（Load unpacked）**
4. 选择本仓库目录：`apps/collector-extension`

## 2. 生成并绑定采集授权码

1. 登录 AI-Native ERP（例如 `http://localhost:3000`），进入 **产品目录 → 1688 采集搬家 & 多平台刊登**
2. 在 **「浏览器一键采集插件授权码」** 卡片中输入名称（如 `Chrome 选品插件`），点击 **生成授权码**
3. 复制仅展示一次的 `erp_col_...` 授权码
4. 打开任意 1688 / 淘宝 / 拼多多 / Alibaba 商品页，点击浏览器右上角本插件图标，填入：
   - **ERP API 地址**：本地为 `http://localhost:8000`（线上部署请填写对应后端 API 域名）
   - **采集授权码**：粘贴刚才复制的 `erp_col_...`
5. 核对自动识别的标题、采购价与规格数，点击 **一键采集到 ERP 选品箱**。
6. 点击成功提示中的 **在 ERP 选品箱中打开 →** 即可直接进入 `/catalog?tab=sourcing` 编辑、入库并多平台一键刊登。

## 3. 本地解析器单测

```bash
node --test apps/collector-extension/tests/parsers.test.mjs
```
