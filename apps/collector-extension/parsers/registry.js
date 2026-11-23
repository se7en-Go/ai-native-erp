import { parse1688Document } from "./alibaba1688.js";
import { detectSourcePlatform, parseGenericDocument } from "./generic.js";

export function parseProductPage(snapshot = {}) {
  const platform = detectSourcePlatform(snapshot.url);
  if (platform === "1688") {
    return parse1688Document(snapshot);
  }
  return parseGenericDocument(snapshot);
}

export function buildCollectPayload(extracted = {}, overrides = {}) {
  const sourcePlatform = overrides.source_platform || extracted.source_platform || "url";
  const cleanPrice =
    overrides.raw_price_cny !== undefined
      ? String(overrides.raw_price_cny).trim()
      : extracted.raw_price_cny
        ? String(extracted.raw_price_cny).trim()
        : null;

  return {
    source_platform: sourcePlatform,
    source_url: overrides.source_url ?? extracted.source_url ?? null,
    raw_title: (overrides.raw_title ?? extracted.raw_title ?? "").trim() || null,
    raw_text: (overrides.raw_text ?? extracted.raw_text ?? "").trim() || null,
    raw_price_cny: cleanPrice || null,
    images: Array.isArray(overrides.images ?? extracted.images)
      ? (overrides.images ?? extracted.images).slice(0, 20)
      : [],
    target_margin_pct: String(overrides.target_margin_pct || "35.00").trim(),
    fx_rate_usd_cny: String(overrides.fx_rate_usd_cny || "7.200000").trim(),
    category: String(overrides.category || "LED Lighting").trim() || "LED Lighting",
    options_spec: Array.isArray(overrides.options_spec ?? extracted.options_spec)
      ? (overrides.options_spec ?? extracted.options_spec).slice(0, 5)
      : [],
    variants_spec: Array.isArray(overrides.variants_spec ?? extracted.variants_spec)
      ? (overrides.variants_spec ?? extracted.variants_spec).slice(0, 50)
      : [],
  };
}

export function resolveEndpoints(apiBaseInput, webBaseInput) {
  const rawApi = (apiBaseInput || "http://localhost:8000").trim().replace(/\/+$/, "");
  const collectUrl = `${rawApi}/api/v1/catalog/sourcing/collect-from-extension`;

  let webOrigin = (webBaseInput || "").trim().replace(/\/+$/, "");
  if (!webOrigin) {
    if (/localhost:8000$/i.test(rawApi)) {
      webOrigin = rawApi.replace(/:8000$/i, ":3000");
    } else if (/127\.0\.0\.1:8000$/i.test(rawApi)) {
      webOrigin = rawApi.replace(/:8000$/i, ":3000");
    } else {
      webOrigin = rawApi;
    }
  }
  return { collectUrl, webOrigin };
}
