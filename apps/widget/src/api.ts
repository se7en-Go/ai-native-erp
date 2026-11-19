export interface WidgetMessage {
  id: string;
  direction: string;
  sender_type: string;
  body_text: string | null;
  created_at: string;
}

export interface PreChatOptIn {
  display_name?: string;
  email?: string;
  whatsapp_phone?: string;
  marketing_opt_in: boolean;
}

export interface WidgetSessionResponse {
  session_token: string;
  visitor_id: string;
  customer_id: string;
  conversation_id: string;
  business_name: string;
  messages: WidgetMessage[];
}

export interface IdentifyPayload {
  email?: string;
  phone_e164?: string;
  external_customer_id?: string;
  display_name?: string;
  timestamp: number;
  hmac_signature: string;
}

export class WidgetApiClient {
  constructor(private readonly apiBase: string) {}

  async createSession(body: {
    site_key: string;
    visitor_id?: string | null;
    page_url: string;
    sku?: string;
    prechat_opt_in?: PreChatOptIn;
  }): Promise<WidgetSessionResponse> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      throw new Error(`Widget session error: ${resp.status}`);
    }
    return (await resp.json()) as WidgetSessionResponse;
  }

  async listMessages(token: string): Promise<WidgetMessage[]> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/messages`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) {
      throw new Error(`Widget list error: ${resp.status}`);
    }
    const data = (await resp.json()) as { items: WidgetMessage[] };
    return data.items;
  }

  async sendMessage(
    token: string,
    bodyText: string,
    pageUrl: string,
    sku?: string,
  ): Promise<WidgetMessage> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        body_text: bodyText,
        page_url: pageUrl,
        sku,
      }),
    });
    if (!resp.ok) {
      throw new Error(`Widget send error: ${resp.status}`);
    }
    return (await resp.json()) as WidgetMessage;
  }

  async identify(
    token: string,
    payload: IdentifyPayload,
  ): Promise<{ session_token: string; customer_id: string; merged: boolean }> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/identify`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      throw new Error(`Widget identify error: ${resp.status}`);
    }
    return (await resp.json()) as {
      session_token: string;
      customer_id: string;
      merged: boolean;
    };
  }
}
