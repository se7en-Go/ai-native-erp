import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/lib/i18n";
import { AuthSessionContext } from "@/components/providers";
import FinancePage from "@/app/finance/page";
import OrdersPage from "@/app/orders/page";
import type { Principal } from "@/lib/contracts";

function wrap(ui: React.ReactNode, role: Principal["role"]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthSessionContext.Provider
        value={{
          principal: {
            user_id: "u1",
            tenant_id: "t1",
            role,
            email: `${role}@qzled.example.com`,
          },
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

describe("Task 5.6 Orders 17TRACK + Inventory Ledger & Finance CVP Margin", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows 70% unpaid warning, enforces finance/owner verification, and renders CVP waterfall", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/finance/payments/pay-30/verify") && init?.method === "POST") {
        return new Response(
          JSON.stringify({ ok: true, order_fulfillment_status: "in_production" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/finance/overview")) {
        return new Response(
          JSON.stringify({
            summary: {
              total_gross_revenue_base: "52332.00",
              total_net_margin_base: "16280.00",
              avg_margin_pct: "31.11",
              base_currency: "CNY",
            },
            receivables: [
              {
                id: "rp-30",
                order_id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                order_mode: "b2b_wholesale",
                milestone_label: "b2b_30_deposit",
                due_amount: "2205.00",
                paid_amount: "2205.00",
                currency: "USD",
                status: "partial",
                shipment_blocked_70_unpaid: true,
                pending_payment_id: "pay-30",
                pending_tx_ref: "TT-20261119-DE",
              },
              {
                id: "rp-70",
                order_id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                order_mode: "b2b_wholesale",
                milestone_label: "b2b_70_balance",
                due_amount: "5145.00",
                paid_amount: "0.00",
                currency: "USD",
                status: "open",
                shipment_blocked_70_unpaid: true,
                pending_payment_id: null,
                pending_tx_ref: null,
              },
            ],
            cvp_snapshots: [
              {
                order_id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                base_currency: "CNY",
                gross_revenue_base: "52332.00",
                cogs_base: "22750.00",
                platform_commission_base: "0.00",
                payment_gateway_fee_base: "250.00",
                shipping_and_duty_base: "12800.00",
                channel_msg_cost_base: "12.00",
                ad_attribution_cost_base: "360.00",
                fx_gain_loss_base: "120.00",
                net_contribution_margin_base: "16280.00",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    wrap(<FinancePage />, "finance");

    await waitFor(() => {
      expect(screen.getAllByTestId("balance-70-unpaid-warning").length).toBeGreaterThan(0);
    });

    expect(screen.getByTestId("cvp-waterfall-ord-b2b-1")).toHaveTextContent("CN¥16,280.00");
    expect(screen.getByTestId("cvp-waterfall-ord-b2b-1").textContent).not.toContain("[object Object]");

    fireEvent.click(screen.getByTestId("verify-payment-btn-pay-30"));

    await waitFor(() => {
      const verifyCall = fetchMock.mock.calls.find((c) =>
        String(c[0]).includes("/finance/payments/pay-30/verify")
      );
      expect(verifyCall).toBeDefined();
    });
  });

  it("blocks agent on /finance and renders 17TRACK 72h stagnation alert + inventory ledger on /orders", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/ai/proposals")) {
        return new Response(JSON.stringify({ items: [], total: 0, limit: 1, offset: 0, has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/orders/inventory/balances")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                warehouse_id: "wh-sz",
                variant_id: "var-1",
                sku: "LED-COB-24V-5M",
                on_hand_qty: 1500,
                reserved_qty: 500,
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/orders/inventory/ledger")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: 101,
                variant_id: "var-1",
                event_type: "reserve_order",
                qty_delta_on_hand: 0,
                qty_delta_reserved: 500,
                reference_type: "order",
                reference_id: "ord-b2b-1",
                idempotency_key: "reserve:ord-b2b-1:var-1",
                created_at: "2026-11-19T08:00:00Z",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/orders/ord-b2b-1")) {
        return new Response(
          JSON.stringify({
            id: "ord-b2b-1",
            order_no: "ORD-B2B-2026-01",
            order_mode: "b2b_wholesale",
            fulfillment_status: "in_production",
            payment_status: "deposit_paid",
            currency: "USD",
            total_amount: "7350.00",
            shipments: [
              {
                id: "ship-1",
                tracking_provider: "17track",
                tracking_number: "DHL99887766",
                carrier_code: "DHL",
                shipment_mode: "air_freight",
                standardized_status: "Alert",
                sub_status: "CustomsHold",
                is_stagnant_72h: true,
                tracking_events: [
                  {
                    time_utc: "2026-11-16T06:00:00Z",
                    location: "Frankfurt DE",
                    description: "Awaiting commercial invoice clearance",
                  },
                ],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.includes("/orders")) {
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "ord-b2b-1",
                order_no: "ORD-B2B-2026-01",
                order_mode: "b2b_wholesale",
                customer_name: "Berlin Lighting GmbH",
                fulfillment_status: "in_production",
                payment_status: "deposit_paid",
                currency: "USD",
                total_amount: "7350.00",
                has_72h_stagnation_alert: true,
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    // Agent blocked on FinancePage
    const { unmount } = wrap(<FinancePage />, "agent");
    expect(screen.getByTestId("rbac-forbidden-guard")).toBeInTheDocument();
    unmount();

    // OrdersPage renders 70%-unpaid shipment block & 17TRACK 72h stagnation alert
    wrap(<OrdersPage />, "manager");

    await waitFor(() => {
      expect(screen.getByText("ORD-B2B-2026-01")).toBeInTheDocument();
      expect(screen.getByTestId("order-72h-alert-ord-b2b-1")).toBeInTheDocument();
      expect(screen.getByTestId("order-ship-blocked-ord-b2b-1")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("open-shipment-drawer-ord-b2b-1"));

    await waitFor(() => {
      expect(screen.getByText("DHL99887766")).toBeInTheDocument();
      expect(screen.getByText(/CustomsHold/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("open-inventory-ledger-btn"));
    await waitFor(() => {
      expect(screen.getByText("reserve:ord-b2b-1:var-1")).toBeInTheDocument();
    });
  });
});
