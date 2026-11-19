import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import { MessagingChannelCard, isMessagingChannelDraftValid } from "@/components/channels/messaging-channel-cards";
import ChannelsSettingsPage from "@/app/settings/channels/page";
import type { Principal } from "@/lib/contracts";

vi.mock("@/lib/data-source", () => ({
  fetchChannelAccounts: vi.fn(async () => ({
    items: [
      {
        id: "ch-messenger-1",
        channel_type: "messenger",
        display_name: "QZ LED Facebook Page",
        external_account_id: "1010101010",
        status: "active",
        rate_limit_profile: { mps: 80 },
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
  fetchChannelDlq: vi.fn(async () => ({ items: [], total: 0, limit: 20, offset: 0, has_more: false })),
  fetchChannelConsents: vi.fn(async () => ({ items: [], total: 0, limit: 20, offset: 0, has_more: false })),
  fetchChannelSuppressions: vi.fn(async () => ({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
  })),
  fetchPendingProposalsCount: vi.fn(async () => ({ total: 0 })),
  replayDlqEvent: vi.fn(async () => ({ status: "replayed" })),
  connectMessagingChannel: vi.fn(async (_kind: string, payload: Record<string, string>) => ({
    channel_account_id: "5e0f0a4c-0000-4000-8000-000000000001",
    external_account_id: payload.ig_account_id ?? payload.page_id ?? payload.bot_token.split(":")[0],
  })),
}));

import { connectMessagingChannel } from "@/lib/data-source";

const principal: Principal = {
  user_id: "u1",
  tenant_id: "t1",
  role: "owner",
  email: "owner@qzled.example.com",
};

function renderWithProviders(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal,
          isLoading: false,
          logout: async () => {},
          refreshSession: async () => {},
        }}
      >
        <I18nProvider defaultLocale="en">{ui}</I18nProvider>
      </AuthSessionContext.Provider>
    </QueryClientProvider>
  );
}

describe("Phase 3D messaging channel connections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("requires a page id, access token and display name before enabling Messenger connect", () => {
    const onSubmit = vi.fn(async () => {});
    renderWithProviders(
      <MessagingChannelCard kind="messenger" connected={false} onSubmit={onSubmit} />
    );

    const button = screen.getByTestId("connect-messenger-btn") as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.change(screen.getByTestId("messenger-page-id-input"), {
      target: { value: "1010101010" },
    });
    fireEvent.change(screen.getByTestId("messenger-access-token-input"), {
      target: { value: "  EAAG-secret-token  " },
    });
    fireEvent.change(screen.getByTestId("messenger-display-name-input"), {
      target: { value: "QZ LED Page" },
    });
    expect(button.disabled).toBe(false);

    fireEvent.click(button);
    expect(onSubmit).toHaveBeenCalledWith({
      page_id: "1010101010",
      page_access_token: "EAAG-secret-token",
      display_name: "QZ LED Page",
    });
  });

  it("rejects Telegram tokens without a bot id and surfaces provider failures", async () => {
    expect(isMessagingChannelDraftValid("telegram", { bot_token: "abc", display_name: "Bot" })).toBe(
      false
    );
    expect(
      isMessagingChannelDraftValid("telegram", {
        bot_token: "123456:AAH-test-token",
        display_name: "Bot",
      })
    ).toBe(true);

    const onSubmit = vi.fn(async () => {
      throw new Error("ERR_INVALID_CHANNEL_CONNECTION");
    });
    renderWithProviders(<MessagingChannelCard kind="telegram" connected={false} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId("telegram-bot-token-input"), {
      target: { value: "123456:AAH-test-token" },
    });
    fireEvent.change(screen.getByTestId("telegram-display-name-input"), {
      target: { value: "Telegram Sales" },
    });
    fireEvent.click(screen.getByTestId("connect-telegram-btn"));

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("ERR_INVALID_CHANNEL_CONNECTION");
    });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("connects a channel from the settings page and refreshes the account list", async () => {
    renderWithProviders(<ChannelsSettingsPage />);

    expect(await screen.findByText("QZ LED Facebook Page")).toBeTruthy();
    fireEvent.change(screen.getByTestId("instagram-account-id-input"), {
      target: { value: "17841400000000000" },
    });
    fireEvent.change(screen.getByTestId("instagram-access-token-input"), {
      target: { value: "IGQVJ-test-token" },
    });
    fireEvent.change(screen.getByTestId("instagram-display-name-input"), {
      target: { value: "QZ LED Instagram" },
    });
    fireEvent.click(screen.getByTestId("connect-instagram_dm-btn"));

    await waitFor(() => {
      expect(connectMessagingChannel).toHaveBeenCalledWith("instagram_dm", {
        ig_account_id: "17841400000000000",
        access_token: "IGQVJ-test-token",
        display_name: "QZ LED Instagram",
      });
    });
    expect((await screen.findByTestId("connect-success-instagram_dm")).textContent).toContain(
      "17841400000000000"
    );
  });
});
