import type { ConversationSummary } from "@/components/inbox/conversation-list";
import type { InboxMessage } from "@/components/inbox/message-thread";
import type { PaginatedResponse } from "@/lib/contracts";

export const mockConversations: ConversationSummary[] = [
  {
    id: "conv-1",
    customer_id: "cust-1",
    customer_name: "Hans Weber",
    country_code: "DE",
    iana_timezone: "Europe/Berlin",
    channel_account_id: "acct-1",
    channel_type: "made_in_china_email",
    conversation_mode: "b2b_inquiry",
    status: "pending_human",
    unread_count: 2,
    sla_due_at: "2026-11-19T12:00:00Z",
    csw_expires_at: "2026-11-20T12:00:00Z",
    human_agent_window_expires_at: null,
    last_message_preview: "Please quote 500 pcs LED-COB-24V-5M FOB Shenzhen",
    updated_at: "2026-11-19T08:00:00Z",
  },
  {
    id: "conv-2",
    customer_id: "cust-2",
    customer_name: "Alice Buyer",
    country_code: "US",
    iana_timezone: "America/New_York",
    channel_account_id: "acct-wa",
    channel_type: "whatsapp_cloud",
    conversation_mode: "b2c_support",
    status: "open",
    unread_count: 1,
    sla_due_at: "2026-11-19T18:00:00Z",
    csw_expires_at: "2026-11-20T18:00:00Z",
    human_agent_window_expires_at: null,
    last_message_preview: "Is the IP67 strip dimmable?",
    updated_at: "2026-11-19T07:30:00Z",
  },
  {
    id: "conv-3",
    customer_id: "cust-3",
    customer_name: "Shop Visitor",
    country_code: "GB",
    iana_timezone: "Europe/London",
    channel_account_id: "acct-web",
    channel_type: "web_livechat",
    conversation_mode: "b2c_support",
    status: "open",
    unread_count: 0,
    sla_due_at: null,
    csw_expires_at: null,
    human_agent_window_expires_at: null,
    last_message_preview: "Thanks, that answers my question.",
    updated_at: "2026-11-19T06:00:00Z",
  },
];

export const mockMessagesByConversation: Record<string, InboxMessage[]> = {
  "conv-1": [
    {
      id: "msg-1",
      conversation_id: "conv-1",
      channel_type: "made_in_china_email",
      direction: "inbound",
      sender_type: "customer",
      content_type: "text",
      body_text: "Need 500 pcs COB LED strip 24V FOB Shenzhen.",
      detected_language: "EN",
      translated_text_zh: "需要 500 条 COB LED 24V 灯带，FOB 深圳报价。",
      delivery_status: "read",
      source_timestamp: "2026-11-19T08:00:00Z",
    },
    {
      id: "msg-2",
      conversation_id: "conv-1",
      channel_type: "made_in_china_email",
      direction: "outbound",
      sender_type: "human_agent",
      content_type: "text",
      body_text: "MOQ 500 tier price is $14.20/pc FOB Shenzhen [doc-led#c2].",
      detected_language: "EN",
      translated_text_zh: "500 条起订价为 14.20 美元/条，FOB 深圳。",
      delivery_status: "delivered",
      source_timestamp: "2026-11-19T08:05:00Z",
    },
  ],
  "conv-2": [
    {
      id: "msg-3",
      conversation_id: "conv-2",
      channel_type: "whatsapp_cloud",
      direction: "inbound",
      sender_type: "customer",
      content_type: "text",
      body_text: "Is the IP67 strip dimmable?",
      detected_language: "EN",
      translated_text_zh: "IP67 灯带可以调光吗？",
      delivery_status: "read",
      source_timestamp: "2026-11-19T07:30:00Z",
    },
  ],
  "conv-3": [
    {
      id: "msg-4",
      conversation_id: "conv-3",
      channel_type: "web_livechat",
      direction: "inbound",
      sender_type: "customer",
      content_type: "text",
      body_text: "Thanks, that answers my question.",
      detected_language: "EN",
      translated_text_zh: "谢谢，这回答了我的问题。",
      delivery_status: "read",
      source_timestamp: "2026-11-19T06:00:00Z",
    },
  ],
};

export function mockConversationsPage(
  channel = "all",
  filter: string = "all",
  query = "",
): PaginatedResponse<ConversationSummary> {
  let items = mockConversations;
  if (channel !== "all") {
    items = items.filter((c) => {
      if (channel === "whatsapp") return c.channel_type === "whatsapp_cloud";
      if (channel === "email")
        return c.channel_type === "email" || c.channel_type === "made_in_china_email";
      return c.channel_type === channel;
    });
  }
  if (filter === "unread") items = items.filter((c) => c.unread_count > 0);
  if (filter === "pending_human") items = items.filter((c) => c.status === "pending_human");
  if (filter === "sla_urgent")
    items = [...items].sort((a, b) =>
      (a.sla_due_at ?? "9999").localeCompare(b.sla_due_at ?? "9999"),
    );
  if (query) {
    const q = query.toLowerCase();
    items = items.filter(
      (c) =>
        c.customer_name.toLowerCase().includes(q) ||
        c.last_message_preview.toLowerCase().includes(q),
    );
  }
  return { items, total: items.length, limit: 25, offset: 0, has_more: false };
}

export function mockMessagesPage(
  conversationId: string,
): PaginatedResponse<InboxMessage> {
  const items = mockMessagesByConversation[conversationId] ?? [];
  return { items, total: items.length, limit: 50, offset: 0, has_more: false };
}
