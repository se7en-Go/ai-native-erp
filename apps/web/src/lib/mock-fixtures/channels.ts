// Structural mirrors of the channel settings shapes (app/settings/channels).
export const mockChannelAccounts = {
  items: [
    {
      id: "ch-1",
      channel_type: "whatsapp_coexistence",
      display_name: "QZ LED WA Business (+86)",
      external_account_id: "waba_phone_1",
      status: "active",
      rate_limit_profile: { mps: 20, coexistence: true },
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};

export const mockChannelDlq = {
  items: [
    {
      id: "dlq-1",
      channel_type: "whatsapp_coexistence",
      external_event_id: "wamid.fail.1",
      error_message: "131056 Pair rate limit exceeded",
      retry_count: 6,
      created_at: "2026-11-19T08:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};

export const mockChannelConsents = {
  items: [
    {
      id: "con-1",
      customer_name: "Elena Vance",
      channel_type: "whatsapp_marketing",
      consent_status: "opted_in",
      proof_source: "shopify_checkout_checkbox",
      recorded_at: "2026-11-19T07:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};

export const mockChannelSuppressions = {
  items: [
    {
      id: "sup-1",
      channel_type: "email",
      normalized_address: "unsubscribe-test@example.com",
      reason: "RFC 8058 One-Click Opt-Out",
      created_at: "2026-11-19T06:00:00Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};
