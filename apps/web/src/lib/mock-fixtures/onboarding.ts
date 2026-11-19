// Structural mirror of OnboardingStatePayload (app/onboarding/page).
export const mockOnboardingState = {
  tenant_id: "t1",
  tenant_slug: "quanzhou-led-factory",
  current_stage: "WorkspaceCreated",
  preset_type: "hybrid_factory_b2b_b2c",
  base_currency: "CNY",
  iana_timezone: "Asia/Shanghai",
  inbound_forwarding_address: "quanzhou-led-factory@inbound.mail.example.com",
  dns_status: { spf_verified: false, dkim_verified: false, return_path_verified: false },
  site_key: "wc_site_qz_001",
  allowed_origins: ["https://shop.qzled.example.com"],
  coexistence_acknowledged: false,
};
