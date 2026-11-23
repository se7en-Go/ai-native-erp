import {
  buildCollectPayload,
  parseProductPage,
  resolveEndpoints,
} from "./parsers/registry.js";

const el = (id) => document.getElementById(id);

let currentExtracted = null;

function showStatus(kind, message, linkHref) {
  const box = el("statusBox");
  box.className = `status ${kind}`;
  box.replaceChildren();
  const textNode = document.createElement("div");
  textNode.textContent = message;
  box.appendChild(textNode);
  if (linkHref) {
    const link = document.createElement("a");
    link.className = "open-link";
    link.href = linkHref;
    link.textContent = "在 ERP 选品箱中打开 →";
    link.addEventListener("click", (ev) => {
      ev.preventDefault();
      chrome.tabs.create({ url: linkHref });
    });
    box.appendChild(link);
  }
}

async function loadSavedSettings() {
  const stored = await chrome.storage.local.get([
    "apiBaseUrl",
    "collectorToken",
    "targetMarginPct",
    "fxRate",
    "category",
  ]);
  el("apiBaseUrl").value = stored.apiBaseUrl || "http://localhost:8000";
  el("collectorToken").value = stored.collectorToken || "";
  if (stored.targetMarginPct) el("targetMarginPct").value = stored.targetMarginPct;
  if (stored.fxRate) el("fxRate").value = stored.fxRate;
  if (stored.category) el("category").value = stored.category;
}

async function saveSettings() {
  await chrome.storage.local.set({
    apiBaseUrl: el("apiBaseUrl").value.trim() || "http://localhost:8000",
    collectorToken: el("collectorToken").value.trim(),
    targetMarginPct: el("targetMarginPct").value.trim() || "35.00",
    fxRate: el("fxRate").value.trim() || "7.200000",
    category: el("category").value.trim() || "LED Lighting",
  });
}

async function captureActiveTabSnapshot(tabId) {
  // Prefer MAIN world scripting to inspect JS state such as 1688 `window.__INIT_DATA` plus DOM.
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        const metas = {};
        document.querySelectorAll("meta").forEach((m) => {
          const k = m.getAttribute("property") || m.getAttribute("name");
          const v = m.getAttribute("content");
          if (k && v) metas[k] = v.trim();
        });
        const jsonLdScripts = Array.from(
          document.querySelectorAll('script[type="application/ld+json"]')
        ).map((s) => s.textContent || "");
        const imgSrcs = Array.from(document.querySelectorAll("img"))
          .map((img) => img.getAttribute("data-orig-src") || img.getAttribute("data-src") || img.src || "")
          .filter((src) => src && !src.startsWith("data:"))
          .slice(0, 30);
        const priceTexts = [];
        document
          .querySelectorAll("[class*='price-text'], [class*='price-num'], [class*='Price--priceText'], [itemprop='price']")
          .forEach((node) => {
            const t = (node.getAttribute("content") || node.textContent || "").trim();
            if (t && /\d/.test(t) && priceTexts.length < 10) priceTexts.push(t);
          });
        const skuTexts = [];
        document
          .querySelectorAll("[class*='sku-item-name'], [class*='prop-item'] span, [class*='sku-item'] [title]")
          .forEach((node) => {
            const t = (node.getAttribute("title") || node.textContent || "").trim();
            if (t && t.length <= 64 && skuTexts.length < 40) skuTexts.push(t);
          });
        return {
          url: window.location.href,
          title: document.title || "",
          metas,
          jsonLdScripts,
          initData: window.__INIT_DATA || window.__GLOBAL_DATA || null,
          imgSrcs,
          priceTexts,
          skuTexts,
          bodyText: (document.body?.innerText || "").slice(0, 1200),
        };
      },
    });
    if (result?.result) return result.result;
  } catch {
    // Fall back to content-script messaging if MAIN-world scripting is blocked on the active page.
  }

  const reply = await chrome.tabs.sendMessage(tabId, { type: "ERP_COLLECT_EXTRACT" });
  return reply?.snapshot || null;
}

async function extractCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url || !/^https?:\/\//i.test(tab.url)) {
    showStatus("err", "当前标签页不是可采集的 HTTP/HTTPS 商品页面。");
    return;
  }

  try {
    const snapshot = await captureActiveTabSnapshot(tab.id);
    if (!snapshot) {
      showStatus("err", "未能读取当前页面 DOM，请刷新页面后重试。");
      return;
    }
    const parsed = parseProductPage(snapshot);
    currentExtracted = parsed;
    el("platformBadge").textContent = (parsed.source_platform || "url").toUpperCase();
    el("rawTitle").value = parsed.raw_title || tab.title || "";
    el("rawPriceCny").value = parsed.raw_price_cny || "";
    el("variantCount").textContent = `规格: ${(parsed.variants_spec || []).length}`;
    el("imageCount").textContent = `图片: ${(parsed.images || []).length}`;
  } catch (err) {
    showStatus("err", `页面提取失败: ${err?.message || String(err)}`);
  }
}

async function submitToErp() {
  await saveSettings();
  const token = el("collectorToken").value.trim();
  if (!token) {
    showStatus("err", "请先填写从 ERP「目录 -> 选品采集」生成的采集授权码。");
    return;
  }
  const basePayload = buildCollectPayload(currentExtracted || {}, {
    raw_title: el("rawTitle").value.trim(),
    raw_price_cny: el("rawPriceCny").value.trim(),
    target_margin_pct: el("targetMarginPct").value.trim() || "35.00",
    fx_rate_usd_cny: el("fxRate").value.trim() || "7.200000",
    category: el("category").value.trim() || "LED Lighting",
  });

  if (!basePayload.raw_title || !basePayload.raw_price_cny) {
    showStatus("err", "请确认商品标题与 CNY 采购价已填写。");
    return;
  }

  const { collectUrl, webOrigin } = resolveEndpoints(el("apiBaseUrl").value);
  const btn = el("collectBtn");
  btn.disabled = true;
  btn.textContent = "正在推送到 ERP…";
  try {
    const resp = await fetch(collectUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Collector-Token": token,
      },
      body: JSON.stringify(basePayload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showStatus("err", `采集失败 (${resp.status}): ${data.detail || data.title || "请求被拒绝"}`);
      return;
    }
    const targetLink = `${webOrigin}${data.catalog_url || "/catalog?tab=sourcing"}`;
    const sku = data.draft?.suggested_sku || "新草稿";
    const usd = data.draft?.target_price_usd ? ` · 建议零售 \$${data.draft.target_price_usd}` : "";
    const mirrored = typeof data.mirrored_images_count === "number"
      ? ` · 转存图片 ${data.mirrored_images_count} 张`
      : "";
    showStatus("ok", `已采集 ${sku}${usd}${mirrored}`, targetLink);
  } catch (err) {
    showStatus("err", `网络请求失败: ${err?.message || String(err)}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "一键采集到 ERP 选品箱";
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadSavedSettings();
  await extractCurrentTab();
  el("refreshBtn").addEventListener("click", extractCurrentTab);
  el("collectBtn").addEventListener("click", submitToErp);
});
