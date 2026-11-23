/**
 * Content script capturing live DOM metadata, gallery images, SKU labels, and inline JSON state.
 * Responds to popup `{ type: "ERP_COLLECT_EXTRACT" }` requests.
 */

(() => {
  function extractInitDataFromScripts() {
    const scripts = document.querySelectorAll("script");
    for (const s of scripts) {
      const txt = s.textContent || "";
      if (!txt.includes("__INIT_DATA")) continue;
      const match = txt.match(/__INIT_DATA\s*=\s*(\{[\s\S]*?\})\s*;?\s*(?:<\/script>|$)/);
      if (match) {
        try {
          return JSON.parse(match[1]);
        } catch {
          // Ignore non-JSON script fragments; MAIN world fallback handles live JS globals.
        }
      }
    }
    return null;
  }

  function extractDomSnapshot() {
    const metas = {};
    document.querySelectorAll("meta").forEach((el) => {
      const key = el.getAttribute("property") || el.getAttribute("name");
      const content = el.getAttribute("content");
      if (key && content) metas[key] = content.trim();
    });

    const jsonLdScripts = Array.from(
      document.querySelectorAll('script[type="application/ld+json"]')
    )
      .map((el) => el.textContent || "")
      .filter((txt) => txt.trim().length > 0);

    const imgSrcs = Array.from(document.querySelectorAll("img"))
      .map(
        (img) =>
          img.getAttribute("data-orig-src") ||
          img.getAttribute("data-src") ||
          img.getAttribute("src") ||
          ""
      )
      .filter((src) => src && !src.startsWith("data:") && /cbu01|alicdn|taobaocdn|pddpic|alibaba|http/i.test(src))
      .slice(0, 30);

    const priceSelectors = [
      "[class*='price-text']",
      "[class*='price-num']",
      "[class*='Price--priceText']",
      "[class*='mod-detail-price'] [class*='value']",
      "[itemprop='price']",
    ];
    const priceTexts = [];
    for (const sel of priceSelectors) {
      document.querySelectorAll(sel).forEach((el) => {
        const text = (el.getAttribute("content") || el.textContent || "").trim();
        if (text && /\d/.test(text) && priceTexts.length < 10) {
          priceTexts.push(text);
        }
      });
    }

    const skuSelectors = [
      "[class*='sku-item-name']",
      "[class*='prop-item'] span",
      "[class*='sku-item'] [title]",
      "[data-sku-id]",
    ];
    const skuTexts = [];
    for (const sel of skuSelectors) {
      document.querySelectorAll(sel).forEach((el) => {
        const text = (el.getAttribute("title") || el.textContent || "").trim();
        if (text && text.length <= 64 && skuTexts.length < 40) {
          skuTexts.push(text);
        }
      });
    }

    return {
      url: window.location.href,
      title: document.title || "",
      metas,
      jsonLdScripts,
      initData: extractInitDataFromScripts(),
      imgSrcs,
      priceTexts,
      skuTexts,
      bodyText: (document.body?.innerText || "").slice(0, 1200),
    };
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "ERP_COLLECT_EXTRACT") {
      sendResponse({ ok: true, snapshot: extractDomSnapshot() });
      return true;
    }
    return false;
  });
})();
