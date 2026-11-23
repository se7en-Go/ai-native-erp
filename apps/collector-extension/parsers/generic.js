/**
 * Generic & multi-source extractor for Taobao/Tmall, Pinduoduo, Alibaba.com, and OpenGraph URLs.
 */

import { normalize1688ImageUrl, normalizeDecimalString } from "./alibaba1688.js";

export function detectSourcePlatform(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string") return "url";
  let host = "";
  try {
    host = new URL(rawUrl).hostname.toLowerCase();
  } catch {
    return "url";
  }
  if (host === "1688.com" || host.endsWith(".1688.com")) return "1688";
  if (
    host === "taobao.com" ||
    host.endsWith(".taobao.com") ||
    host === "tmall.com" ||
    host.endsWith(".tmall.com")
  ) {
    return "taobao";
  }
  if (
    host === "yangkeduo.com" ||
    host.endsWith(".yangkeduo.com") ||
    host === "pinduoduo.com" ||
    host.endsWith(".pinduoduo.com")
  ) {
    return "pdd";
  }
  if (host === "alibaba.com" || host.endsWith(".alibaba.com")) return "alibaba";
  return "url";
}

function extractProductFromJsonLd(jsonLdScripts = []) {
  for (const raw of jsonLdScripts) {
    if (!raw || typeof raw !== "string") continue;
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      continue;
    }
    const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
    while (queue.length > 0) {
      const node = queue.shift();
      if (!node || typeof node !== "object") continue;
      if (Array.isArray(node["@graph"])) {
        queue.push(...node["@graph"]);
      }
      const types = Array.isArray(node["@type"]) ? node["@type"] : [node["@type"]];
      if (types.some((t) => String(t || "").toLowerCase() === "product")) {
        return node;
      }
    }
  }
  return null;
}

function extractOfferPrice(offers) {
  if (!offers) return { price: null, currency: null };
  const list = Array.isArray(offers) ? offers : [offers];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const rawPrice = item.price ?? item.lowPrice ?? item.highPrice;
    const parsed = normalizeDecimalString(rawPrice);
    if (parsed) {
      return {
        price: parsed,
        currency: item.priceCurrency ? String(item.priceCurrency).toUpperCase() : null,
      };
    }
  }
  return { price: null, currency: null };
}

export function parseGenericDocument(snapshot = {}) {
  const sourcePlatform = detectSourcePlatform(snapshot.url);
  const metas = snapshot.metas || {};
  const jsonLdProduct = extractProductFromJsonLd(snapshot.jsonLdScripts || []);

  const rawTitle = String(
    jsonLdProduct?.name ||
      metas["og:title"] ||
      metas["twitter:title"] ||
      snapshot.title ||
      ""
  ).trim();

  const ldOffer = extractOfferPrice(jsonLdProduct?.offers);
  let rawPriceCny = ldOffer.price || normalizeDecimalString(metas["product:price:amount"]);
  if (!rawPriceCny) {
    for (const txt of snapshot.priceTexts || []) {
      const parsed = normalizeDecimalString(txt);
      if (parsed) {
        rawPriceCny = parsed;
        break;
      }
    }
  }

  const ldImages = Array.isArray(jsonLdProduct?.image)
    ? jsonLdProduct.image
    : jsonLdProduct?.image
      ? [jsonLdProduct.image]
      : [];

  const seen = new Set();
  const images = [];
  for (const candidate of [
    ...ldImages,
    metas["og:image"],
    metas["twitter:image"],
    ...(snapshot.imgSrcs || []),
  ]) {
    const clean = normalize1688ImageUrl(
      typeof candidate === "string" ? candidate : candidate?.url || candidate?.contentUrl
    );
    if (clean && !seen.has(clean)) {
      seen.add(clean);
      images.push(clean);
      if (images.length >= 10) break;
    }
  }

  const description = String(
    jsonLdProduct?.description || metas["og:description"] || metas.description || ""
  ).trim();

  const textParts = [];
  if (rawTitle) textParts.push(rawTitle);
  if (rawPriceCny) {
    textParts.push(ldOffer.currency ? `${ldOffer.currency} ${rawPriceCny}` : `¥${rawPriceCny}`);
  }
  if (description) textParts.push(description);
  if (snapshot.bodyText && textParts.length < 2) textParts.push(snapshot.bodyText.slice(0, 600));

  const variantsSpec = [];
  const optionsSpec = [];
  if (rawPriceCny && Array.isArray(snapshot.skuTexts) && snapshot.skuTexts.length > 0) {
    const cleanSpecs = Array.from(
      new Set(snapshot.skuTexts.map((s) => String(s || "").trim()).filter(Boolean))
    ).slice(0, 20);
    if (cleanSpecs.length > 0) {
      optionsSpec.push({ name: "Spec", values: cleanSpecs });
      cleanSpecs.forEach((val, idx) => {
        const ascii = val
          .toUpperCase()
          .replace(/[^A-Z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "");
        variantsSpec.push({
          sku_suffix: ascii && ascii.length <= 24 ? ascii : `V${idx + 1}`,
          option_values: { Spec: val },
          cost_price_cny: rawPriceCny,
          image_url: null,
          initial_stock: 100,
        });
      });
    }
  }

  return {
    source_platform: sourcePlatform,
    source_url: snapshot.url || null,
    raw_title: rawTitle || null,
    raw_text: textParts.join("\n").slice(0, 20000) || null,
    raw_price_cny: rawPriceCny,
    images,
    options_spec: optionsSpec,
    variants_spec: variantsSpec,
  };
}
