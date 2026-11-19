import type {
  IdentifyPayload,
  PreChatOptIn,
  WidgetApiClient,
  WidgetMessage,
} from "./api";
import { WidgetApiClient as WidgetApiClientImpl } from "./api";

export interface WidgetConfig {
  siteKey: string;
  apiBase: string;
  businessName?: string;
  sku?: string;
}

const WIDGET_STYLES = `
  :host { all: initial; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .launcher { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000; width: 56px; height: 56px;
    border-radius: 9999px; border: none; background: #0f172a; color: #fff; cursor: pointer;
    box-shadow: 0 10px 25px rgba(15, 23, 42, 0.25); font-size: 20px; }
  .panel { position: fixed; right: 20px; bottom: 88px; z-index: 2147483000; width: 340px; max-height: 480px;
    display: none; flex-direction: column; background: #ffffff; border-radius: 14px;
    border: 1px solid #e2e8f0; box-shadow: 0 18px 40px rgba(15, 23, 42, 0.22); overflow: hidden; }
  .panel.open { display: flex; }
  .header { background: #0f172a; color: #ffffff; padding: 14px 16px; display: flex;
    justify-content: space-between; align-items: center; font-size: 14px; font-weight: 600; }
  .close-btn { background: transparent; border: none; color: #cbd5e1; cursor: pointer; font-size: 18px; }
  .body { padding: 12px; overflow-y: auto; flex: 1; background: #f8fafc; display: flex; flex-direction: column; gap: 8px; min-height: 220px; }
  .msg { max-width: 82%; padding: 8px 11px; border-radius: 10px; font-size: 13px; line-height: 1.4; }
  .msg.inbound { align-self: flex-end; background: #2563eb; color: #ffffff; }
  .msg.outbound { align-self: flex-start; background: #ffffff; color: #0f172a; border: 1px solid #e2e8f0; }
  .prechat { display: flex; flex-direction: column; gap: 8px; padding: 12px; background: #ffffff; border-bottom: 1px solid #e2e8f0; }
  .input { width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13px; }
  .optin { display: flex; align-items: flex-start; gap: 6px; font-size: 11px; color: #475569; }
  .footer { display: flex; gap: 6px; padding: 10px; border-top: 1px solid #e2e8f0; background: #ffffff; }
  .send-btn { background: #2563eb; color: #ffffff; border: none; border-radius: 8px; padding: 8px 12px; font-size: 13px; cursor: pointer; }
`;

export class LiveChatWidgetApp {
  private readonly api: WidgetApiClient;
  private readonly shadow: ShadowRoot;
  private readonly storageKey: string;
  private sessionToken: string | null = null;
  private visitorId: string | null = null;
  private businessName: string;
  private messages: WidgetMessage[] = [];
  private isOpen = false;
  private pollTimer: number | null = null;

  private panelEl!: HTMLDivElement;
  private bodyEl!: HTMLDivElement;
  private prechatEl!: HTMLDivElement;
  private inputEl!: HTMLInputElement;

  constructor(private readonly config: WidgetConfig) {
    this.api = new WidgetApiClientImpl(config.apiBase.replace(/\/$/, ""));
    this.storageKey = `erp_livechat_${config.siteKey}`;
    this.businessName = config.businessName ?? "Support";
    this.visitorId = localStorage.getItem(`${this.storageKey}_vid`);

    const host = document.createElement("div");
    host.id = "erp-livechat-host";
    document.body.appendChild(host);
    this.shadow = host.attachShadow({ mode: "open" });
    this.renderShell();
  }

  private renderShell(): void {
    const style = document.createElement("style");
    style.textContent = WIDGET_STYLES;

    const launcher = document.createElement("button");
    launcher.className = "launcher";
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open LiveChat");
    launcher.textContent = "Chat";
    launcher.addEventListener("click", () => void this.toggle());

    this.panelEl = document.createElement("div");
    this.panelEl.className = "panel";

    const header = document.createElement("div");
    header.className = "header";
    const titleSpan = document.createElement("span");
    titleSpan.textContent = this.businessName;
    const closeBtn = document.createElement("button");
    closeBtn.className = "close-btn";
    closeBtn.type = "button";
    closeBtn.textContent = "x";
    closeBtn.addEventListener("click", () => this.close());
    header.append(titleSpan, closeBtn);

    this.prechatEl = document.createElement("div");
    this.prechatEl.className = "prechat";
    const emailInput = document.createElement("input");
    emailInput.className = "input";
    emailInput.placeholder = "Work Email (for quote follow-up)";
    const waInput = document.createElement("input");
    waInput.className = "input";
    waInput.placeholder = "WhatsApp Number (+1...)";
    const optinLabel = document.createElement("label");
    optinLabel.className = "optin";
    const optinCheckbox = document.createElement("input");
    optinCheckbox.type = "checkbox";
    optinCheckbox.checked = false; // Explicitly NEVER pre-checked per GDPR/WhatsApp rules
    const optinText = document.createElement("span");
    optinText.textContent = `I agree to receive order & quote updates from ${this.businessName}.`;
    optinLabel.append(optinCheckbox, optinText);

    const startBtn = document.createElement("button");
    startBtn.className = "send-btn";
    startBtn.type = "button";
    startBtn.textContent = "Start Chat";
    startBtn.addEventListener("click", () => {
      const prechat: PreChatOptIn = {
        email: emailInput.value.trim() || undefined,
        whatsapp_phone: waInput.value.trim() || undefined,
        marketing_opt_in: optinCheckbox.checked,
      };
      void this.ensureSession(prechat);
    });
    this.prechatEl.append(emailInput, waInput, optinLabel, startBtn);

    this.bodyEl = document.createElement("div");
    this.bodyEl.className = "body";

    const footer = document.createElement("form");
    footer.className = "footer";
    this.inputEl = document.createElement("input");
    this.inputEl.className = "input";
    this.inputEl.placeholder = "Type a message...";
    const sendBtn = document.createElement("button");
    sendBtn.className = "send-btn";
    sendBtn.type = "submit";
    sendBtn.textContent = "Send";
    footer.append(this.inputEl, sendBtn);
    footer.addEventListener("submit", (ev) => {
      ev.preventDefault();
      void this.handleSend();
    });

    this.panelEl.append(header, this.prechatEl, this.bodyEl, footer);
    this.shadow.append(style, launcher, this.panelEl);
  }

  async open(): Promise<void> {
    this.isOpen = true;
    this.panelEl.classList.add("open");
    if (!this.sessionToken) {
      await this.ensureSession();
    }
    this.startPolling();
  }

  close(): void {
    this.isOpen = false;
    this.panelEl.classList.remove("open");
    if (this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  async toggle(): Promise<void> {
    if (this.isOpen) {
      this.close();
    } else {
      await this.open();
    }
  }

  private async ensureSession(prechat?: PreChatOptIn): Promise<void> {
    const resp = await this.api.createSession({
      site_key: this.config.siteKey,
      visitor_id: this.visitorId,
      page_url: window.location.href,
      sku: this.config.sku,
      prechat_opt_in: prechat,
    });
    this.sessionToken = resp.session_token;
    this.visitorId = resp.visitor_id;
    this.businessName = resp.business_name;
    localStorage.setItem(`${this.storageKey}_vid`, resp.visitor_id);
    if (prechat && (prechat.email || prechat.whatsapp_phone)) {
      this.prechatEl.style.display = "none";
    }
    this.messages = resp.messages;
    this.renderMessages();
  }

  private async handleSend(): Promise<void> {
    const text = this.inputEl.value.trim();
    if (!text) return;
    if (!this.sessionToken) {
      await this.ensureSession();
    }
    if (!this.sessionToken) return;
    this.inputEl.value = "";
    const msg = await this.api.sendMessage(
      this.sessionToken,
      text,
      window.location.href,
      this.config.sku,
    );
    this.messages.push(msg);
    this.renderMessages();
  }

  async identify(payload: IdentifyPayload): Promise<void> {
    if (!this.sessionToken) {
      await this.ensureSession();
    }
    if (!this.sessionToken) return;
    const upgraded = await this.api.identify(this.sessionToken, payload);
    this.sessionToken = upgraded.session_token;
  }

  private startPolling(): void {
    if (this.pollTimer !== null) return;
    this.pollTimer = window.setInterval(() => {
      void (async () => {
        if (!this.sessionToken || !this.isOpen) return;
        this.messages = await this.api.listMessages(this.sessionToken);
        this.renderMessages();
      })();
    }, 4000);
  }

  private renderMessages(): void {
    this.bodyEl.textContent = "";
    for (const msg of this.messages) {
      const bubble = document.createElement("div");
      bubble.className = `msg ${msg.direction === "inbound" ? "inbound" : "outbound"}`;
      bubble.textContent = msg.body_text ?? "";
      this.bodyEl.appendChild(bubble);
    }
    this.bodyEl.scrollTop = this.bodyEl.scrollHeight;
  }
}
