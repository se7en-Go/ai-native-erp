"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

interface SsePayload {
  conversation_id?: string;
  order_id?: string;
  proposal_id?: string;
}

function safeParseJson(raw: string): SsePayload {
  try {
    return JSON.parse(raw) as SsePayload;
  } catch {
    return {};
  }
}

export function useRealtimeInbox(enabled: boolean = true) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") {
      return;
    }

    const source = new EventSource("/api/bff/v1/inbox/events", {
      withCredentials: true,
    });

    const onMessageCreated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      if (payload.conversation_id) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "messages", payload.conversation_id],
        });
      }
    };

    const onConversationUpdated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      if (payload.conversation_id) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "conversation", payload.conversation_id],
        });
      }
    };

    const onProposalEvent = () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "proposals"] });
      void queryClient.invalidateQueries({ queryKey: ["ai", "overview"] });
    };

    const onShipmentUpdated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      if (payload.order_id) {
        void queryClient.invalidateQueries({ queryKey: ["orders", payload.order_id] });
      }
    };

    const onWalletUpdated = () => {
      void queryClient.invalidateQueries({ queryKey: ["ai", "overview"] });
      void queryClient.invalidateQueries({ queryKey: ["ai", "wallet"] });
    };

    const onDlqUpdated = () => {
      void queryClient.invalidateQueries({ queryKey: ["channels", "dlq"] });
    };

    source.addEventListener("message.created", onMessageCreated as EventListener);
    source.addEventListener("conversation.updated", onConversationUpdated as EventListener);
    source.addEventListener("proposal.created", onProposalEvent as EventListener);
    source.addEventListener("proposal.resolved", onProposalEvent as EventListener);
    source.addEventListener("shipment.updated", onShipmentUpdated as EventListener);
    source.addEventListener("wallet.updated", onWalletUpdated as EventListener);
    source.addEventListener("dlq.updated", onDlqUpdated as EventListener);

    return () => {
      source.close();
    };
  }, [enabled, queryClient]);
}
