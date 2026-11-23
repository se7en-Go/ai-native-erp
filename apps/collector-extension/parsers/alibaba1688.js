/**
 * Pure parser for 1688 offer pages (`detail.1688.com`, `m.1688.com`, etc.).
 * Works both on injected `window.__INIT_DATA` structures and DOM snapshots.
 */

export function normalize1688ImageUrl(rawUrl) {
  if (typeof rawUrl !== "string") return null;
  let url = rawUrl.trim();
  if (!url || url.startsWith("data:") || url.startsWith("blob:")) return null;
  if (url.startsWith("//")) url = `https:${url}`;
  if (url.startsWith("http://") && /alicdn\.com|1688\.com/i.test(url)) {
    url = url.replace(/^http:\/\//i, "https://");
  }
  if (!/^https?:\/\//i.test(url)) return null;
  // Strip 1688 / alicdn thumbnail resize suffixes such as `.60x60.jpg` or `.sum.jpg`.
  url = url
    .replace(/(\.(?:jpe?g|png|webp))_\.\w+$/i, "$1")
    .replace(/\.(\d{2,4}x\d{2,4})(\.(?:jpe?g|png|webp))$/i, "$2")
    .replace(/\.sum(\.(?:jpe?g|png|webp))$/i, "$1")
    .replace(/\.search(\.(?:jpe?g|png|webp))$/i, "$1");
  return url;
}

export function normalizeDecimalString(rawValue) {
  if (typeof rawValue === "number" && Number.isFinite(rawValue) && rawValue > 0) {
    return rawValue.toFixed(2);
  }
  if (typeof rawValue !== "string") return null;
  const match = rawValue.replace(/,/g, "").match(/(\d+(?:\.\d{1,4})?)/);
  if (!match) return null;
  const num = Number(match[1]);
  if (!Number.isFinite(num) || num <= 0) return null;
  return num.toFixed(2);
}

function dedupeImages(candidates, limit = 10) {
  const seen = new Set();
  const out = [];
  for (const item of candidates || []) {
    const clean = normalize1688ImageUrl(item);
    if (clean && !seen.has(clean)) {
      seen.add(clean);
      out.push(clean);
      if (out.length >= limit) break;
    }
  }
  return out;
}

function find1688Models(initData) {
  if (!initData || typeof initData !== "object") {
    return { tempModel: null, skuModel: null, images: [], rawNodes: [] };
  }
  let tempModel = initData.globalData?.tempModel ?? initData.tempModel ?? null;
  let skuModel = initData.globalData?.skuModel ?? initData.skuModel ?? null;
  let images = [];
  const rawNodes = [];

  if (Array.isArray(initData.globalData?.images)) {
    for (const img of initData.globalData.images) {
      images.push(typeof img === "string" ? img : img?.fullPathImageURI || img?.imageURI || img?.url);
    }
  }

  const dataMap = initData.data;
  if (dataMap && typeof dataMap === "object") {
    for (const node of Object.values(dataMap)) {
      if (!node || typeof node !== "object") continue;
      rawNodes.push(node);
      const inner = node.data && typeof node.data === "object" ? node.data : node;
      if (!tempModel && inner.tempModel) tempModel = inner.tempModel;
      if (!skuModel && inner.skuModel) skuModel = inner.skuModel;
      if (!skuModel && (inner.skuProps || inner.skuInfoMap)) skuModel = inner;
      if (Array.isArray(inner.offerImgList)) {
        for (const img of inner.offerImgList) {
          images.push(typeof img === "string" ? img : img?.originalImageURI || img?.fullPathImageURI || img?.url);
        }
      }
      if (Array.isArray(inner.imageList)) {
        for (const img of inner.imageList) {
          images.push(typeof img === "string" ? img : img?.fullPathImageURI || img?.size310x310ImageURI || img?.url);
        }
      }
    }
  }

  return { tempModel, skuModel, images, rawNodes };
}

function makeSkuSuffix(label, index) {
  const ascii = String(label || "")
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (ascii && ascii.length <= 24) return ascii;
  return `V${index + 1}`;
}

export function parse1688Document(snapshot = {}) {
  const { tempModel, skuModel, images: initImages } = find1688Models(snapshot.initData);
  const metas = snapshot.metas || {};

  const rawTitle = String(
    tempModel?.offerTitle ||
      tempModel?.title ||
      snapshot.initData?.globalData?.offerTitle ||
      metas["og:title"] ||
      snapshot.title ||
      ""
  )
    .replace(/\s*-\s*阿里巴巴.*$/i, "")
    .trim();

  const optionProps = Array.isArray(skuModel?.skuProps) ? skuModel.skuProps : [];
  const optionsSpec = optionProps
    .map((prop) => {
      const name = String(prop?.prop || prop?.name || "").trim();
      const values = Array.isArray(prop?.value)
        ? prop.value.map((v) => String(v?.name || v || "").trim()).filter(Boolean)
        : [];
      return name && values.length > 0 ? { name: name.slice(0, 64), values: Array.from(new Set(values)).slice(0, 50) } : null;
    })
    .filter(Boolean)
    .slice(0, 5);

  const propImageByValue = new Map();
  for (const prop of optionProps) {
    for (const v of prop?.value || []) {
      if (v?.name && v?.imageUrl) {
        const clean = normalize1688ImageUrl(v.imageUrl);
        if (clean) propImageByValue.set(String(v.name).trim(), clean);
      }
    }
  }

  const skuInfoMap = skuModel?.skuInfoMap || skuModel?.skuMap || {};
  const fallbackSkuPrice = normalizeDecimalString(
    skuModel?.skuPriceScale ||
      tempModel?.priceRanges?.[0]?.price ||
      tempModel?.minPrice ||
      snapshot.priceTexts?.[0]
  );

  const variantsSpec = [];
  const usedSuffixes = new Set();
  if (skuInfoMap && typeof skuInfoMap === "object") {
    const entries = Object.entries(skuInfoMap).slice(0, 50);
    for (const [idx, [specKey, entry]] of entries.entries()) {
      if (!entry || typeof entry !== "object") continue;
      const parts = String(entry.specAttrs || specKey)
        .split("&gt;")
        .flatMap((s) => s.split(">"))
        .map((s) => s.trim())
        .filter(Boolean);
      const optionValues = {};
      if (optionsSpec.length > 0 && parts.length > 0) {
        optionsSpec.forEach((opt, optIdx) => {
          optionValues[opt.name] = parts[optIdx] || parts[0] || `Option ${idx + 1}`;
        });
      } else if (parts.length > 0) {
        optionValues.Spec = parts.join(" / ");
      } else {
        optionValues.Spec = `Variant ${idx + 1}`;
      }

      const costCny =
        normalizeDecimalString(entry.price ?? entry.discountPrice ?? entry.retailPrice) ||
        fallbackSkuPrice;
      if (!costCny) continue;

      let suffix = makeSkuSuffix(
        entry.skuId ? `${parts.join("-") || "SKU"}-${String(entry.skuId).slice(-4)}` : parts.join("-"),
        idx
      );
      if (usedSuffixes.has(suffix)) {
        suffix = `${suffix}-${idx + 1}`.slice(0, 40);
      }
      usedSuffixes.add(suffix);

      let variantImg = normalize1688ImageUrl(entry.imageUrl || entry.skuImageUrl);
      if (!variantImg) {
        for (const p of parts) {
          if (propImageByValue.has(p)) {
            variantImg = propImageByValue.get(p);
            break;
          }
        }
      }

      const canBook = Number.parseInt(String(entry.canBookCount ?? entry.stock ?? 100), 10);
      variantsSpec.push({
        sku_suffix: suffix,
        option_values: optionValues,
        cost_price_cny: costCny,
        image_url: variantImg || null,
        initial_stock: Number.isFinite(canBook) && canBook >= 0 ? Math.min(canBook, 1_000_000) : 100,
      });
    }
  }

  let rawPriceCny = normalizeDecimalString(
    tempModel?.priceRanges?.[0]?.price ||
      tempModel?.minPrice ||
      skuModel?.skuPriceScale ||
      metas["product:price:amount"]
  );
  if (!rawPriceCny && variantsSpec.length > 0) {
    rawPriceCny = variantsSpec
      .map((v) => Number(v.cost_price_cny))
      .reduce((min, cur) => Math.min(min, cur), Number(variantsSpec[0].cost_price_cny))
      .toFixed(2);
  }
  if (!rawPriceCny) {
    for (const txt of snapshot.priceTexts || []) {
      const parsed = normalizeDecimalString(txt);
      if (parsed) {
        rawPriceCny = parsed;
        break;
      }
    }
  }

  // DOM fallback for SKU tags when script state is unavailable.
  if (variantsSpec.length === 0 && Array.isArray(snapshot.skuTexts) && snapshot.skuTexts.length > 0 && rawPriceCny) {
    const cleanDomSpecs = Array.from(
      new Set(snapshot.skuTexts.map((s) => String(s || "").trim()).filter(Boolean))
    ).slice(0, 20);
    if (cleanDomSpecs.length > 0 && optionsSpec.length === 0) {
      optionsSpec.push({ name: "Spec", values: cleanDomSpecs });
    }
    cleanDomSpecs.forEach((label, idx) => {
      variantsSpec.push({
        sku_suffix: makeSkuSuffix(label, idx),
        option_values: { [optionsSpec[0]?.name || "Spec"]: label },
        cost_price_cny: rawPriceCny,
        image_url: null,
        initial_stock: 100,
      });
    });
  }

  const allImages = dedupeImages([
    ...initImages,
    ...variantsSpec.map((v) => v.image_url).filter(Boolean),
    ...(metas["og:image"] ? [metas["og:image"]] : []),
    ...(snapshot.imgSrcs || []),
  ]);

  const rawTextParts = [];
  if (rawTitle) rawTextParts.push(rawTitle);
  if (rawPriceCny) rawTextParts.push(`¥${rawPriceCny}`);
  if (tempModel?.beginAmount) rawTextParts.push(`MOQ: ${tempModel.beginAmount}`);
  if (metas["og:description"]) rawTextParts.push(metas["og:description"]);
  if (snapshot.bodyText && rawTextParts.length < 2) rawTextParts.push(snapshot.bodyText.slice(0, 600));

  return {
    source_platform: "1688",
    source_url: snapshot.url || null,
    raw_title: rawTitle || null,
    raw_text: rawTextParts.join("\n").slice(0, 20000) || null,
    raw_price_cny: rawPriceCny,
    images: allImages,
    options_spec: optionsSpec,
    variants_spec: variantsSpec,
  };
}
