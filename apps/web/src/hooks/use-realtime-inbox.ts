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

    // Contract SSE endpoint is /api/v1/inbox/stream (there is no
    // /api/v1/inbox/events in contracts/openapi.json); the BFF proxies it
    // and serves a mock heartbeat on the same path in mock mode.
    const source = new EventSource("/api/bff/v1/inbox/stream", {
      withCredentials: true,
    });

    let hadOpen = false;
    let hadError = false;

    // After any drop (or a second open) reconcile the inbox baseline, because
    // Pub/Sub has no replay: events published while the client was offline are
    // gone, so the only safe recovery is a full refetch.
    const reconcileInboxBaseline = () => {
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["inbox", "messages"] });
    };

    const onOpen = () => {
      if (hadOpen || hadError) {
        reconcileInboxBaseline();
      }
      hadOpen = true;
      hadError = false;
    };

    const onError = () => {
      hadError = true;
    };

    const onMessageCreated = (ev: MessageEvent) => {
      const payload = safeParseJson(ev.data);
      void queryClient.invalidateQueries({ queryKey: ["inbox", "conversations"] });
      if (payload.conversation_id) {
        void queryClient.invalidateQueries({
          queryKey: ["inbox", "messages", payload.conversation_id],
        });
      }
    };

    const onMessageStatusUpdated = (ev: MessageEvent) => {
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
    source.addEventListener(
      "message.status_updated",
      onMessageStatusUpdated as EventListener
    );
    source.addEventListener("conversation.updated", onConversationUpdated as EventListener);
    source.addEventListener("proposal.created", onProposalEvent as EventListener);
    source.addEventListener("proposal.resolved", onProposalEvent as EventListener);
    source.addEventListener("shipment.updated", onShipmentUpdated as EventListener);
    source.addEventListener("wallet.updated", onWalletUpdated as EventListener);
    source.addEventListener("dlq.updated", onDlqUpdated as EventListener);
    source.addEventListener("open", onOpen as EventListener);
    source.addEventListener("error", onError as EventListener);

    let closed = false;
    const closeSource = () => {
      if (closed) return;
      closed = true;
      source.close();
    };

    window.addEventListener("pagehide", closeSource);
    window.addEventListener("beforeunload", closeSource);

    return () => {
      window.removeEventListener("pagehide", closeSource);
      window.removeEventListener("beforeunload", closeSource);
      closeSource();
    };
  }, [enabled, queryClient]);
}
