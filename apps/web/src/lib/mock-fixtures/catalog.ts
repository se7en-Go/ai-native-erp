// Structural mirrors of CatalogVariantItem / PlatformSkuMappingItem from
// components/catalog/sku-mapping-drawer (defined here so fixtures stay usable
// without importing UI components).
export interface MockMoqTier {
  min_qty: number;
  unit_price_usd: number;
}

export interface MockCatalogVariant {
  id: string;
  product_id: string;
  spu_code: string;
  category: string;
  hs_code?: string | null;
  certifications: string[];
  sku: string;
  attributes: Record<string, string>;
  cost_price_base?: string | null;
  base_currency: string;
  min_margin_floor_pct: string;
  retail_price_usd: string;
  b2b_moq_tiers: MockMoqTier[];
  lead_time_days: number;
}

export interface MockPlatformSkuMapping {
  id: string;
  variant_id: string;
  channel_account_id: string;
  channel_label?: string;
  external_product_id: string;
  external_sku: string;
  bundle_multiplier: number;
  sync_inventory_enabled: boolean;
}

export const mockVariants: MockCatalogVariant[] = [
  {
    id: "var-1",
    product_id: "prod-1",
    spu_code: "LED-COB-24V",
    category: "LED Strip Lighting",
    hs_code: "94054200",
    certifications: ["CE", "RoHS", "UL"],
    sku: "LED-COB-24V-5M",
    attributes: { cct: "4000K", ip_rating: "IP65" },
    cost_price_base: "45.50",
    base_currency: "CNY",
    min_margin_floor_pct: "15.00",
    retail_price_usd: "29.90",
    b2b_moq_tiers: [
      { min_qty: 50, unit_price_usd: 18.5 },
      { min_qty: 500, unit_price_usd: 14.2 },
    ],
    lead_time_days: 7,
  },
  {
    id: "var-2",
    product_id: "prod-2",
    spu_code: "LED-PANEL-60",
    category: "LED Panel Lighting",
    hs_code: "94051100",
    certifications: ["CE", "RoHS"],
    sku: "LED-PANEL-60-40W",
    attributes: { cct: "5000K", ip_rating: "IP20" },
    cost_price_base: "68.00",
    base_currency: "CNY",
    min_margin_floor_pct: "18.00",
    retail_price_usd: "49.90",
    b2b_moq_tiers: [
      { min_qty: 100, unit_price_usd: 32.0 },
      { min_qty: 1000, unit_price_usd: 26.5 },
    ],
    lead_time_days: 10,
  },
];

export const mockVariantMappings: MockPlatformSkuMapping[] = [
  {
    id: "map-1",
    variant_id: "var-1",
    channel_account_id: "ch-shop-1",
    channel_label: "Shopify US Flagship",
    external_product_id: "gid://shopify/Product/991",
    external_sku: "COB-24V-2PACK",
    bundle_multiplier: 2,
    sync_inventory_enabled: true,
  },
  {
    id: "map-2",
    variant_id: "var-1",
    channel_account_id: "ch-amz-1",
    channel_label: "Amazon US",
    external_product_id: "B0COB24V5M",
    external_sku: "COB-24V-5M-AMZ",
    bundle_multiplier: 1,
    sync_inventory_enabled: true,
  },
];

export function mockVariantsPage(query: string): {
  items: MockCatalogVariant[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
} {
  const q = query.trim().toLowerCase();
  const items = q
    ? mockVariants.filter((v) =>
        [v.sku, v.spu_code, v.hs_code ?? "", v.category]
          .join(" ")
          .toLowerCase()
          .includes(q),
      )
    : mockVariants;
  return { items, total: items.length, limit: 50, offset: 0, has_more: false };
}

export function mockVariantDetail(variantId: string): {
  variant: MockCatalogVariant;
  mappings: MockPlatformSkuMapping[];
} {
  const variant =
    mockVariants.find((v) => v.id === variantId) ?? mockVariants[0];
  return {
    variant,
    mappings: mockVariantMappings.filter((m) => m.variant_id === variant.id),
  };
}
