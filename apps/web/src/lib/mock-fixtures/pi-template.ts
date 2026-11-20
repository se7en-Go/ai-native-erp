// Structural mirror of PiTemplateSettings (lib/api-client/live-client) plus the
// demo PI email delivery receipt, so the whole "发 PI 邮件" loop is walkable
// with NEXT_PUBLIC_USE_MOCK_API=true and zero backend.
import type {
  PiTemplateSettings,
  SendQuotePiEmailPayload,
  SendQuotePiEmailResponse,
} from "@/lib/api-client/live-client";

export const mockPiTemplateSettings: PiTemplateSettings = {
  company_legal_name: "Quanzhou Smart LED Lighting Co., Ltd.",
  company_address:
    "No. 88 Photoelectric Industrial Park, Nan'an, Quanzhou, Fujian, 362300, China",
  contact_phone: "+86 595 2688 0000",
  contact_email: "sales@301go.net",
  website: "https://www.301go.net",
  tax_or_reg_no: "91350583MA2XQZLED8",
  bank_beneficiary_name: "Quanzhou Smart LED Lighting Co., Ltd.",
  bank_name: "Bank of China, Quanzhou Branch",
  bank_account_no: "4188 0000 0000 1234",
  bank_swift_code: "BKCHCNBJ73A",
  bank_address: "No. 188 Fengze Street, Fengze District, Quanzhou, Fujian, China",
  default_terms_notes:
    "1. 30% T/T deposit, 70% balance before shipment.\n2. Lead time 7-15 days after deposit.\n3. FX locked for 48 hours.\n4. HS code, brand and model must match customs documents.",
  footer_signature_title: "Export Sales Manager",
};

/** 演示用发送回执：字段与 POST /b2b/quotes/{id}/send-email 响应一致。 */
export function mockSendPiEmailResponse(
  quoteId: string,
  quoteNo: string,
  payload: SendQuotePiEmailPayload,
): SendQuotePiEmailResponse {
  const filename = `${quoteNo}-${(payload.language ?? "en").toUpperCase()}.pdf`;
  return {
    quote_id: quoteId,
    quote_no: quoteNo,
    recipient_email: payload.recipient_email,
    from_email: "sales@301go.net",
    provider: payload.channel_account_id === "mock-email-account" ? "resend" : "smtp_imap",
    external_message_id: `mock-resend-${quoteNo}-${Date.now()}`,
    pdf_filename: filename,
    pdf_size_bytes: 184_320,
    quote_status: "sent",
  };
}
