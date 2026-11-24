// AI 多语种改写 + 草稿文案 / 平台类目编辑的前端契约。
//
// 三层验证：
//   1. 常量层：12 种语言 / 4 种语气 / 16 个平台的默认语言，与后端
//      `erp_core/catalog/sourcing_copy_protocol.py` 完全一致；
//   2. AI 改写层：切 Shopee 自动带出 `th`、切 Ozon 自动带出 `ru`，提交必须打到
//      `POST /api/bff/v1/catalog/sourcing/drafts/{id}/ai-rewrite`，
//      `rewrite_mode` 为 `llm` 时显示绿色 LLM 徽标，
//      为 `deterministic_fallback` 时显示琥珀色回退徽标且绝不声称 LLM；
//   3. 编辑层：改标题 / USD 零售价 / Shopee 类目 ID `100630` 后保存，必须打到
//      `PATCH /api/bff/v1/catalog/sourcing/drafts/{id}`，请求体带
//      `marketplace_category_attrs.shopee.category_id === "100630"`。
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CatalogPage from "@/app/catalog/page";
import { AuthSessionContext, type AuthSessionState } from "@/components/providers";
import { I18nProvider } from "@/lib/i18n";
import type { Principal } from "@/lib/contracts";
import type { SourcingDraftRead } from "@/lib/api-client/types.gen";
import {
  DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL,
  REWRITE_LANGUAGE_OPTIONS,
  REWRITE_TONE_OPTIONS,
  SOURCING_DEFAULT_FX_RATE,
  SOURCING_DEFAULT_MARGIN_PCT,
  aiRewriteSourcingDraft,
  fetchSourcingDrafts,
  planSourcingDraft,
  updateSourcingDraft,
} from "@/lib/api-client/pack-f-sourcing";

vi.mock("@/hooks/use-realtime-inbox", () => ({ useRealtimeInbox: vi.fn() }));

const BFF = "/api/bff";
const DRAFT_ID = "11111111-2222-3333-4444-555555555555";
const AI_REWRITE_URL = `${BFF}/v1/catalog/sourcing/drafts/${DRAFT_ID}/ai-rewrite`;
const DRAFT_PATCH_URL = `${BFF}/v1/catalog/sourcing/drafts/${DRAFT_ID}`;

interface CapturedCall {
  url: string;
  method: string;
  body: string | null;
}

interface FakeBackend {
  draft: SourcingDraftRead;
  rewriteMode: "llm" | "deterministic_fallback";
  fallbackReason: string;
  calls: CapturedCall[];
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 现有草稿：已保存一条 `lazada:ms` 马来语文案，用来验证多语种芯片与预览。 */
function baseDraft(): SourcingDraftRead {
  const draft = planSourcingDraft(
    {
      source_platform: "1688",
      source_url: "https://detail.1688.com/offer/680001234567.html",
      raw_title: "COB LED Strip 24V 5m 4000K",
      raw_price_cny: "18.60",
      fx_rate_usd_cny: SOURCING_DEFAULT_FX_RATE,
      target_margin_pct: SOURCING_DEFAULT_MARGIN_PCT,
      category: "LED Strip Lighting",
      hs_code: "94054200",
    },
    { id: DRAFT_ID, now: "2026-11-24T00:00:00.000Z" },
  );
  return {
    ...draft,
    localized_copies: {
      "lazada:ms": {
        title: "Jalur LED COB 24V 5m 4000K",
        bullet_points: ["Harga kilang terus", "MOQ 100 pcs"],
        target_channel: "lazada",
        target_language: "ms",
        generated_at: "2026-11-24T00:00:00.000Z",
      },
    },
  };
}

/**
 * 一个最小但有状态的假后端：改写会真的写进 `localized_copies`，
 * PATCH 会真的改草稿，所以重新拉取草稿列表后 UI 必须真的显示新数据。
 */
function installFakeBackend(initial: SourcingDraftRead): FakeBackend {
  const backend: FakeBackend = {
    draft: initial,
    rewriteMode: "llm",
    fallbackReason: "ai_rewriter_not_configured",
    calls: [],
  };

  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: CapturedCall =
      input instanceof Request
        ? {
            url: input.url,
            method: (input.method || "GET").toUpperCase(),
            body: await input.text(),
          }
        : {
            url: String(input),
            method: (init?.method || "GET").toUpperCase(),
            body: typeof init?.body === "string" ? init.body : null,
          };
    backend.calls.push(call);
    const path = call.url.startsWith(BFF) ? call.url.slice(BFF.length) : call.url;

    if (call.method === "POST" && path.endsWith("/ai-rewrite")) {
      const body = JSON.parse(call.body ?? "{}") as {
        target_channel?: string;
        target_language?: string;
        tone?: string;
        apply_to_draft?: boolean;
      };
      const channel = (body.target_channel ?? "shopify").trim().toLowerCase();
      const language = (body.target_language ?? "en").trim().toLowerCase();
      const tone = body.tone ?? "conversion";
      const isLlm = backend.rewriteMode === "llm";
      const rewrite = {
        title: `${backend.draft.optimized_title_en} · ${channel} ${language}`,
        bullet_points: ["b1", "b2", "b3", "b4", "b5"],
        description: `${channel} ${language} localized description`,
        target_channel: channel,
        target_language: language,
        tone,
        model_used: isLlm ? "gpt-4o-mini" : "deterministic-rule-v1",
        rewrite_mode: backend.rewriteMode,
        credits_consumed: isLlm ? "0.0120" : "0.0000",
        fallback_reason: isLlm ? null : backend.fallbackReason,
        seo_keywords: ["led strip"],
        generated_at: "2026-11-24T00:00:02.000Z",
      };
      const applyToDraft = body.apply_to_draft !== false;
      backend.draft = {
        ...backend.draft,
        localized_copies: {
          ...(backend.draft.localized_copies ?? {}),
          [`${channel}:${language}`]: rewrite,
        },
        optimized_title_en: applyToDraft
          ? rewrite.title
          : backend.draft.optimized_title_en,
        bullet_points: applyToDraft ? rewrite.bullet_points : backend.draft.bullet_points,
        updated_at: "2026-11-24T00:00:02.000Z",
      };
      return jsonResponse({ draft: backend.draft, rewrite });
    }

    if (call.method === "PATCH" && /^\/v1\/catalog\/sourcing\/drafts\/[^/]+$/.test(path)) {
      const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
      backend.draft = {
        ...backend.draft,
        ...(typeof body.optimized_title_en === "string"
          ? { optimized_title_en: body.optimized_title_en }
          : {}),
        ...(body.target_price_usd != null
          ? { target_price_usd: String(body.target_price_usd) }
          : {}),
        ...(Array.isArray(body.bullet_points)
          ? { bullet_points: body.bullet_points as string[] }
          : {}),
        ...(typeof body.suggested_category === "string"
          ? { suggested_category: body.suggested_category }
          : {}),
        ...(typeof body.suggested_hs_code === "string"
          ? { suggested_hs_code: body.suggested_hs_code }
          : {}),
        ...(body.marketplace_category_attrs != null
          ? {
              marketplace_category_attrs:
                body.marketplace_category_attrs as SourcingDraftRead["marketplace_category_attrs"],
            }
          : {}),
        updated_at: "2026-11-24T00:00:03.000Z",
      };
      return jsonResponse(backend.draft);
    }

    if (call.method === "GET" && path === "/v1/catalog/sourcing/drafts") {
      return jsonResponse([backend.draft]);
    }
    if (path.startsWith("/v1/catalog/sourcing/collector-tokens")) {
      return jsonResponse([]);
    }
    if (path.startsWith("/v1/catalog/variants")) {
      return jsonResponse({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
    }
    if (path.startsWith("/v1/channels/accounts")) {
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
    }
    if (path.startsWith("/v1/ai/proposals")) {
      return jsonResponse({ items: [], total: 0, limit: 1, offset: 0, has_more: false });
    }
    return jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
  });

  vi.stubGlobal("fetch", mock);
  return backend;
}

function principal(role: Principal["role"]): Principal {
  return {
    user_id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    role,
    email: `${role}@qzled.example.com`,
    tenant_slug: "qz-led-factory",
    onboarding_stage: "LiveProduction",
  };
}

function renderCatalog(role: Principal["role"], locale: "zh" | "en" = "zh") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const authValue: AuthSessionState = {
    principal: principal(role),
    isLoading: false,
    logout: vi.fn(async () => {}),
    refreshSession: vi.fn(async () => {}),
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthSessionContext.Provider value={authValue}>
        <I18nProvider defaultLocale={locale}>
          <CatalogPage />
        </I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("sourcing rewrite contract constants", () => {
  it("mirrors the backend language / tone / per-marketplace default contract", () => {
    expect(REWRITE_LANGUAGE_OPTIONS.map((option) => option.value)).toEqual([
      "en",
      "zh",
      "es",
      "pt",
      "ru",
      "th",
      "vi",
      "id",
      "ms",
      "fr",
      "de",
      "ja",
    ]);
    for (const option of REWRITE_LANGUAGE_OPTIONS) {
      expect(option.zh.length).toBeGreaterThan(0);
      expect(option.en.length).toBeGreaterThan(0);
    }

    expect(REWRITE_TONE_OPTIONS.map((option) => option.value)).toEqual([
      "conversion",
      "seo",
      "b2b_wholesale",
      "social_viral",
    ]);

    expect(Object.keys(DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL)).toHaveLength(16);
    expect(DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL.shopee).toBe("th");
    expect(DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL.lazada).toBe("ms");
    expect(DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL.ozon).toBe("ru");
    expect(DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL.mercado_libre).toBe("es");
    for (const channel of [
      "shopify",
      "amazon_sp",
      "ebay",
      "walmart",
      "woocommerce",
      "shopline",
      "shoplazza",
      "alibaba_email",
      "tiktok_shop",
      "temu",
      "shein",
      "aliexpress",
    ] as const) {
      expect(DEFAULT_REWRITE_LANGUAGE_BY_CHANNEL[channel]).toBe("en");
    }
  });
});

describe("catalog sourcing AI rewrite & draft edit panels", () => {
  beforeEach(() => {
    // Live 模式：只有真的走 `bffFetch` 才能断言 POST / PATCH 的端点与请求体。
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "false");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("defaults the language per marketplace, posts the rewrite and never fakes an LLM badge", async () => {
    const backend = installFakeBackend(baseDraft());
    renderCatalog("owner");

    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));
    const card = await screen.findByTestId(`sourcing-draft-${DRAFT_ID}`);

    // 1) 已保存的多语种文案芯片 + 点击预览。
    const chip = within(card).getByTestId(`sourcing-localized-chip-${DRAFT_ID}-lazada:ms`);
    expect(chip.textContent).toContain("lazada:ms");
    expect(chip.textContent).toContain("Lazada (MS)");
    fireEvent.click(chip);
    const preview = await within(card).findByTestId(
      `sourcing-localized-preview-${DRAFT_ID}-lazada:ms`,
    );
    expect(preview.textContent).toContain("Jalur LED COB");

    // 2) 打开 AI 多语种改写面板。
    fireEvent.click(within(card).getByTestId(`sourcing-ai-rewrite-toggle-${DRAFT_ID}`));
    const channelSelect = await screen.findByTestId(`sourcing-ai-channel-${DRAFT_ID}`);
    expect(channelSelect).toHaveValue("shopify");
    expect(screen.getByTestId(`sourcing-ai-lang-${DRAFT_ID}`)).toHaveValue("en");

    fireEvent.change(channelSelect, { target: { value: "shopee" } });
    expect(screen.getByTestId(`sourcing-ai-lang-${DRAFT_ID}`)).toHaveValue("th");

    fireEvent.change(screen.getByTestId(`sourcing-ai-channel-${DRAFT_ID}`), {
      target: { value: "ozon" },
    });
    expect(screen.getByTestId(`sourcing-ai-lang-${DRAFT_ID}`)).toHaveValue("ru");

    fireEvent.change(screen.getByTestId(`sourcing-ai-channel-${DRAFT_ID}`), {
      target: { value: "mercado_libre" },
    });
    expect(screen.getByTestId(`sourcing-ai-lang-${DRAFT_ID}`)).toHaveValue("es");

    // 回到 Shopee：默认语言必须重新是泰语，语气可选，默认勾选应用到草稿。
    fireEvent.change(screen.getByTestId(`sourcing-ai-channel-${DRAFT_ID}`), {
      target: { value: "shopee" },
    });
    fireEvent.change(screen.getByTestId(`sourcing-ai-tone-${DRAFT_ID}`), {
      target: { value: "conversion" },
    });
    expect(screen.getByTestId(`sourcing-ai-apply-${DRAFT_ID}`)).toBeChecked();

    fireEvent.click(screen.getByTestId(`sourcing-ai-rewrite-${DRAFT_ID}`));

    await waitFor(() => {
      expect(backend.calls.some((call) => call.url === AI_REWRITE_URL)).toBe(true);
    });
    const postCall = backend.calls.find((call) => call.url === AI_REWRITE_URL);
    expect(postCall?.method).toBe("POST");
    expect(JSON.parse(postCall?.body ?? "{}")).toEqual({
      target_channel: "shopee",
      target_language: "th",
      tone: "conversion",
      apply_to_draft: true,
    });

    // 3) LLM 模式：绿色徽标 + 真实模型名 + credits。
    const result = await screen.findByTestId(`sourcing-ai-rewrite-result-${DRAFT_ID}`);
    const llmBadge = within(result).getByTestId(`sourcing-ai-rewrite-mode-${DRAFT_ID}`);
    expect(llmBadge.textContent).toContain("LLM 实时改写");
    expect(llmBadge.textContent).toContain("gpt-4o-mini");
    expect(llmBadge.textContent).toContain("0.0120");
    expect(llmBadge.className).toContain("emerald");

    // 改写结果真的落到草稿的多语种文案库里（重新拉取后芯片出现）。
    await waitFor(() => {
      expect(
        within(screen.getByTestId(`sourcing-draft-${DRAFT_ID}`)).getByTestId(
          `sourcing-localized-chip-${DRAFT_ID}-shopee:th`,
        ),
      ).toBeTruthy();
    });

    // 4) 确定性回退模式：琥珀色徽标，绝不出现 “LLM”。
    backend.rewriteMode = "deterministic_fallback";
    fireEvent.change(screen.getByTestId(`sourcing-ai-channel-${DRAFT_ID}`), {
      target: { value: "ozon" },
    });
    expect(screen.getByTestId(`sourcing-ai-lang-${DRAFT_ID}`)).toHaveValue("ru");
    fireEvent.click(screen.getByTestId(`sourcing-ai-rewrite-${DRAFT_ID}`));

    await waitFor(() => {
      expect(
        screen.getByTestId(`sourcing-ai-rewrite-mode-${DRAFT_ID}`).textContent,
      ).toContain("确定性本地规则回退");
    });
    const fallbackBadge = screen.getByTestId(`sourcing-ai-rewrite-mode-${DRAFT_ID}`);
    expect(fallbackBadge.textContent).toContain("ai_rewriter_not_configured");
    expect(fallbackBadge.textContent).toContain("0 credits");
    expect(fallbackBadge.textContent).not.toContain("LLM");
    expect(fallbackBadge.className).toContain("amber");
  });

  it("saves the edited copy and the Shopee category id through PATCH", async () => {
    const draft = baseDraft();
    const backend = installFakeBackend(draft);
    renderCatalog("owner");

    fireEvent.click(await screen.findByTestId("catalog-tab-sourcing"));
    const card = await screen.findByTestId(`sourcing-draft-${DRAFT_ID}`);

    fireEvent.click(within(card).getByTestId(`sourcing-edit-toggle-${DRAFT_ID}`));
    const titleInput = await screen.findByTestId(`sourcing-edit-title-${DRAFT_ID}`);
    expect(titleInput).toHaveValue(draft.optimized_title_en);
    expect(screen.getByTestId(`sourcing-edit-price-${DRAFT_ID}`)).toHaveValue(
      Number(draft.target_price_usd),
    );
    expect(screen.getByTestId(`sourcing-edit-cat-channel-${DRAFT_ID}`)).toHaveValue("shopify");
    expect(screen.getByTestId(`sourcing-edit-cat-id-${DRAFT_ID}`)).toHaveValue("");

    fireEvent.change(titleInput, {
      target: { value: "COB LED Strip 24V 5m 4000K | Shopee TH Hero Listing" },
    });
    fireEvent.change(screen.getByTestId(`sourcing-edit-price-${DRAFT_ID}`), {
      target: { value: "54.90" },
    });
    fireEvent.change(screen.getByTestId(`sourcing-edit-cat-channel-${DRAFT_ID}`), {
      target: { value: "shopee" },
    });
    fireEvent.change(screen.getByTestId(`sourcing-edit-cat-id-${DRAFT_ID}`), {
      target: { value: "100630" },
    });
    expect(screen.getByTestId(`sourcing-edit-bullets-${DRAFT_ID}`)).toHaveValue(
      draft.bullet_points.join("\n"),
    );

    fireEvent.click(screen.getByTestId(`sourcing-edit-save-${DRAFT_ID}`));

    await waitFor(() => {
      expect(backend.calls.some((call) => call.method === "PATCH")).toBe(true);
    });
    const patchCall = backend.calls.find((call) => call.method === "PATCH");
    expect(patchCall?.url).toBe(DRAFT_PATCH_URL);
    const payload = JSON.parse(patchCall?.body ?? "{}");

    expect(payload.optimized_title_en).toBe(
      "COB LED Strip 24V 5m 4000K | Shopee TH Hero Listing",
    );
    expect(payload.target_price_usd).toBe("54.90");
    expect(payload.marketplace_category_attrs.shopee.category_id).toBe("100630");
    expect(payload.suggested_hs_code).toBe("94054200");
    expect(payload.bullet_points).toHaveLength(5);

    // 保存后重新拉取草稿：卡片标题与类目属性随之后端数据刷新。
    await waitFor(() => {
      expect(screen.getByTestId(`sourcing-draft-${DRAFT_ID}`).textContent).toContain(
        "Shopee TH Hero Listing",
      );
    });
    expect(backend.draft.marketplace_category_attrs?.shopee?.category_id).toBe("100630");
    await waitFor(() => {
      expect(screen.getByTestId(`sourcing-edit-saved-${DRAFT_ID}`).textContent).toContain(
        "已保存草稿改动",
      );
    });
  });
});

// ---------------------------------------------------------------------------
// Mock 演示模式：零后端也要能跑通，且必须诚实标注「确定性回退 / 0 credits」。
// ---------------------------------------------------------------------------

describe("mock sourcing AI rewrite store", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCK_API", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("seeds clean nested objects, stores rewrites and patches copy honestly", async () => {
    const seeded = await fetchSourcingDrafts();
    expect(seeded.length).toBeGreaterThan(0);
    for (const draft of seeded) {
      expect(draft.localized_copies).toEqual(expect.any(Object));
      expect(draft.marketplace_category_attrs).toEqual(expect.any(Object));
    }

    const rewrite = await aiRewriteSourcingDraft("src-draft-1", {
      target_channel: "shopee",
      target_language: "th",
      tone: "seo",
      apply_to_draft: true,
    });
    // Mock 绝不冒充 LLM。
    expect(rewrite.rewrite.rewrite_mode).toBe("deterministic_fallback");
    expect(rewrite.rewrite.model_used).toBe("deterministic-rule-v1");
    expect(rewrite.rewrite.credits_consumed).toBe("0.0000");
    expect(rewrite.rewrite.fallback_reason).toBe("mock_demo_mode");
    expect(rewrite.rewrite.target_channel).toBe("shopee");
    expect(rewrite.rewrite.target_language).toBe("th");
    expect(rewrite.rewrite.bullet_points).toHaveLength(5);
    expect(rewrite.draft.localized_copies?.["shopee:th"]).toBeTruthy();

    const patched = await updateSourcingDraft("src-draft-1", {
      optimized_title_en: "Shopee TH hero listing",
      target_price_usd: "42.50",
      marketplace_category_attrs: { shopee: { category_id: "100630" } },
    });
    expect(patched.optimized_title_en).toBe("Shopee TH hero listing");
    expect(patched.target_price_usd).toBe("42.50");
    expect(patched.marketplace_category_attrs?.shopee?.category_id).toBe("100630");

    const after = (await fetchSourcingDrafts()).find((draft) => draft.id === "src-draft-1");
    expect(after?.optimized_title_en).toBe("Shopee TH hero listing");
    expect(after?.target_price_usd).toBe("42.50");
    expect(after?.localized_copies?.["shopee:th"]).toBeTruthy();
  });
});
