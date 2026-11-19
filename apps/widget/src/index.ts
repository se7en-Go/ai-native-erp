import type { IdentifyPayload } from "./api";
import type { WidgetConfig } from "./widget-app";
import { LiveChatWidgetApp } from "./widget-app";

let activeInstance: LiveChatWidgetApp | null = null;

const ErpLiveChat = {
  init(config: WidgetConfig): LiveChatWidgetApp {
    if (!activeInstance) {
      activeInstance = new LiveChatWidgetApp(config);
    }
    return activeInstance;
  },
  open(): void {
    void activeInstance?.open();
  },
  close(): void {
    activeInstance?.close();
  },
  identify(payload: IdentifyPayload): Promise<void> {
    if (!activeInstance) {
      return Promise.resolve();
    }
    return activeInstance.identify(payload);
  },
};

export default ErpLiveChat;

declare global {
  interface Window {
    ErpLiveChat?: typeof ErpLiveChat;
  }
}

window.ErpLiveChat = ErpLiveChat;

const currentScript = document.currentScript as HTMLScriptElement | null;
if (currentScript?.dataset.siteKey && currentScript?.dataset.apiBase) {
  ErpLiveChat.init({
    siteKey: currentScript.dataset.siteKey,
    apiBase: currentScript.dataset.apiBase,
    businessName: currentScript.dataset.businessName,
    sku: currentScript.dataset.sku,
  });
}
