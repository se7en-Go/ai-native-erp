import assert from "node:assert/strict";
import test from "node:test";

import { normalize1688ImageUrl, parse1688Document } from "../parsers/alibaba1688.js";
import { detectSourcePlatform, parseGenericDocument } from "../parsers/generic.js";
import {
  buildCollectPayload,
  parseProductPage,
  resolveEndpoints,
} from "../parsers/registry.js";

test("normalize1688ImageUrl upgrades protocol and strips thumbnail suffixes", () => {
  assert.equal(
    normalize1688ImageUrl("//cbu01.alicdn.com/img/ibank/O1CN01Test.400x400.jpg"),
    "https://cbu01.alicdn.com/img/ibank/O1CN01Test.jpg"
  );
  assert.equal(
    normalize1688ImageUrl("http://cbu01.alicdn.com/img/ibank/O1CN01Sum.sum.png"),
    "https://cbu01.alicdn.com/img/ibank/O1CN01Sum.png"
  );
  assert.equal(normalize1688ImageUrl("data:image/png;base64,AAA"), null);
});

test("parse1688Document extracts multi-variant INIT_DATA and price tiers", () => {
  const result = parse1688Document({
    url: "https://detail.1688.com/offer/77889900.html",
    title: "户外LED太阳能投光灯 跨境批发 - 阿里巴巴",
    initData: {
      globalData: {
        tempModel: {
          offerTitle: "户外LED太阳能投光灯 100W 防水IP66",
          beginAmount: 5,
          priceRanges: [{ price: "28.50", beginAmount: 5 }],
        },
        skuModel: {
          skuProps: [
            {
              prop: "Color",
              value: [
                { name: "Warm White 3000K", imageUrl: "//cbu01.alicdn.com/img/warm.60x60.jpg" },
                { name: "Cool White 6500K", imageUrl: "//cbu01.alicdn.com/img/cool.60x60.jpg" },
              ],
            },
          ],
          skuInfoMap: {
            "Warm White 3000K": {
              skuId: "1001",
              specAttrs: "Warm White 3000K",
              price: "28.50",
              canBookCount: 240,
            },
            "Cool White 6500K": {
              skuId: "1002",
              specAttrs: "Cool White 6500K",
              price: "31.00",
              canBookCount: 180,
            },
          },
        },
        images: [
          { fullPathImageURI: "//cbu01.alicdn.com/img/main1.jpg" },
          { fullPathImageURI: "https://cbu01.alicdn.com/img/main2.jpg" },
        ],
      },
    },
  });

  assert.equal(result.source_platform, "1688");
  assert.equal(result.raw_title, "户外LED太阳能投光灯 100W 防水IP66");
  assert.equal(result.raw_price_cny, "28.50");
  assert.equal(result.options_spec.length, 1);
  assert.deepEqual(result.options_spec[0], {
    name: "Color",
    values: ["Warm White 3000K", "Cool White 6500K"],
  });
  assert.equal(result.variants_spec.length, 2);
  assert.equal(result.variants_spec[0].cost_price_cny, "28.50");
  assert.equal(result.variants_spec[0].image_url, "https://cbu01.alicdn.com/img/warm.jpg");
  assert.equal(result.variants_spec[0].initial_stock, 240);
  assert.equal(result.images[0], "https://cbu01.alicdn.com/img/main1.jpg");
});

test("parse1688Document falls back cleanly to DOM snapshot without INIT_DATA", () => {
  const result = parse1688Document({
    url: "https://detail.1688.com/offer/112233.html",
    title: "LED Strip 24V Waterproof - 阿里巴巴",
    metas: { "og:image": "https://cbu01.alicdn.com/img/dom-main.jpg" },
    priceTexts: ["¥15.80"],
    skuTexts: ["5M", "10M"],
  });

  assert.equal(result.source_platform, "1688");
  assert.equal(result.raw_title, "LED Strip 24V Waterproof");
  assert.equal(result.raw_price_cny, "15.80");
  assert.equal(result.variants_spec.length, 2);
  assert.equal(result.variants_spec[1].sku_suffix, "10M");
});

test("parseProductPage handles Taobao, PDD, and Alibaba JSON-LD / OpenGraph pages", () => {
  assert.equal(detectSourcePlatform("https://item.taobao.com/item.htm?id=123"), "taobao");
  assert.equal(detectSourcePlatform("https://mobile.yangkeduo.com/goods.html?goods_id=1"), "pdd");
  assert.equal(detectSourcePlatform("https://www.alibaba.com/product-detail/led_123.html"), "alibaba");

  const tb = parseProductPage({
    url: "https://item.taobao.com/item.htm?id=999",
    jsonLdScripts: [
      JSON.stringify({
        "@type": "Product",
        name: "Taobao LED Desk Lamp",
        image: ["https://img.alicdn.com/lamp.jpg"],
        offers: { "@type": "Offer", price: "49.9", priceCurrency: "CNY" },
      }),
    ],
  });
  assert.equal(tb.source_platform, "taobao");
  assert.equal(tb.raw_title, "Taobao LED Desk Lamp");
  assert.equal(tb.raw_price_cny, "49.90");
  assert.deepEqual(tb.images, ["https://img.alicdn.com/lamp.jpg"]);

  const pdd = parseGenericDocument({
    url: "https://mobile.yangkeduo.com/goods.html?goods_id=55",
    metas: {
      "og:title": "PDD Mini COB Keychain Light",
      "og:image": "https://img.pddpic.com/cob.jpg",
      "product:price:amount": "9.90",
    },
  });
  const payload = buildCollectPayload(pdd);
  assert.equal(payload.source_platform, "pdd");
  assert.equal(payload.raw_price_cny, "9.90");
  assert.equal(payload.target_margin_pct, "35.00");

  const endpoints = resolveEndpoints("http://localhost:8000/");
  assert.equal(
    endpoints.collectUrl,
    "http://localhost:8000/api/v1/catalog/sourcing/collect-from-extension"
  );
  assert.equal(endpoints.webOrigin, "http://localhost:3000");
});
