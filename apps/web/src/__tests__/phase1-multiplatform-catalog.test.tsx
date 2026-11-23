import { describe, it, expect, vi, afterEach } from "vitest";
import {
  planSourcingDraft,
  publishMasterProduct,
  uploadCatalogMedia,
  sourcingStoreLabel,
} from "@/lib/api-client/pack-f-sourcing";

describe("Phase 1 Multiplatform Media & Variants helpers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("computes multi-variant Decimal USD prices cleanly inside planSourcingDraft", () => {
    const draft = planSourcingDraft({
      source_platform: "1688",
      source_url: "https://detail.1688.com/offer/123456.html",
      raw_title: "COB Strip 24V Dual CCT",
      fx_rate_usd_cny: "7.200000",
      target_margin_pct: "40.00",
      variants_spec: [
        { sku_suffix: "3000K", option_values: { CCT: "3000K" }, cost_price_cny: "36.00" },
        { sku_suffix: "6000K", option_values: { CCT: "6000K" }, cost_price_cny: "43.20" },
      ],
    });
    expect(draft.variants_spec).toHaveLength(2);
    expect(draft.variants_spec?.[0].retail_price_usd).toBe("8.33");
    expect(draft.variants_spec?.[1].retail_price_usd).toBe("10.00");
  });

  it("exposes localized labels for all 8 publishable storefronts", () => {
    for (const type of [
      "shopify",
      "tiktok_shop",
      "amazon_sp",
      "ebay",
      "alibaba_email",
      "woocommerce",
      "shopline",
      "shoplazza",
    ]) {
      expect(sourcingStoreLabel(type).length).toBeGreaterThan(2);
    }
  });

  it("uploads multipart media via /api/bff/v1/catalog/media/upload", async () => {
    const mockFetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          id: "asset-1",
          tenant_id: "t1",
          storage_key: "tenants/t1/media/abc.png",
          sha256: "a".repeat(64),
          mime_type: "image/png",
          byte_size: 64,
          content_path: "/api/v1/catalog/media/asset-1/content",
          created_at: "2026-11-23T00:00:00Z",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", mockFetch);

    const file = new File(["fake"], "strip.png", { type: "image/png" });
    const res = await uploadCatalogMedia(file, { sourcingDraftId: "draft-1" });
    expect(res.mime_type).toBe("image/png");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("calls /v1/catalog/products/{id}/publish via publishMasterProduct", async () => {
    const mockFetch = vi.fn(async () => {
      return new Response(
        JSON.stringify([
          {
            channel_account_id: "ch-1",
            channel_type: "tiktok_shop",
            external_sku: "TIKTOKSHOP-M1",
            external_product_id: "tt-prod-1",
            price_usd: "12.50",
            stock_sync_ratio: "1.00",
            mapping_id: "map-1",
            published_at: "2026-11-23T00:00:00Z",
            listing_state: "live_published",
            external_listing_confirmed: true,
            publish_mode: "live_marketplace_api",
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", mockFetch);

    const res = await publishMasterProduct("prod-1", {
      channel_account_ids: ["ch-1"],
      push_to_marketplace: true,
    });
    expect(res[0].external_product_id).toBe("tt-prod-1");
  });
});
