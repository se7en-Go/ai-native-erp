# Meta Tech Provider & Embedded Signup v4/v5 App Review Submission

**Status:** Day-1 external review package (submission-ready text; English answers are the ones to paste into App Review).
**Product:** `ai-native-erp` — multi-tenant B2C + B2B cross-border commerce workspace with an L0–L2 gated AI employee runtime.
**Scope of this request:** WhatsApp Business Platform (Cloud API) via Embedded Signup v4/v5, plus Messenger and Instagram messaging/comment permissions.
**Official references used:** [Embedded Signup — onboard WhatsApp Business app users (Coexistence)](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/), [Embedded Signup versions](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/), [Instagram API with Unified Login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-unified-login), [Messenger Platform messaging limits](https://developers.facebook.com/docs/messenger-platform/reference/send-api#limits), [WhatsApp pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing).

## 1. Entity and app identifiers

The values below are legal-identity fields that only the applicant's corporate records can supply. Everything else in this document is final review copy.

| Field | Value for the submission form |
| :--- | :--- |
| Business Portfolio name | `{{LEGAL_ENTITY_NAME}}` |
| Business verification status | Verified Business Portfolio (Meta Business Suite → Security Center) |
| Registered address | `{{REGISTERED_ADDRESS}}` |
| Company registration number | `{{COMPANY_REGISTRATION_NUMBER}}` |
| App display name | AI Native ERP |
| App category | Business and Pages / Commerce |
| App platform | Web (Next.js BFF + FastAPI backend) |
| Privacy Policy URL | `{{PRIVACY_POLICY_URL}}` |
| Terms of Service URL | `{{TERMS_OF_SERVICE_URL}}` |
| Data Deletion Instructions URL | `{{DATA_DELETION_URL}}` |
| Privacy/DPO contact | `{{PRIVACY_CONTACT_EMAIL}}` |
| Reviewer test tenant | Demo workspace `quanzhou-led-factory` (Quanzhou Smart LED Factory), seeded with the public sample knowledge base in `04-hybrid-factory-kb-seed-pack.md` |

## 2. What the app does (one paragraph for the reviewer)

AI Native ERP is a multi-tenant business workspace for cross-border sellers and export manufacturers. A merchant connects its own WhatsApp Business Account (WABA), Facebook Page, and Instagram Professional account, and then handles customer conversations, orders, quotations, and logistics in one inbox. A policy-gated AI assistant drafts or sends replies inside the tenant's configured autonomy level: L2 auto-send for low-risk intents (order status, logistics, published FAQ), L1 proposal-only for commercial documents such as quotations, and L0 human-only for payment confirmation and contract negotiation. Every outbound message is filtered by a compliance layer that blocks off-platform contact leakage on marketplace channels and forbids automated use of the `HUMAN_AGENT` tag.

## 3. Requested permissions and per-permission use-case answers

Paste each block into the matching App Review "Explain how you will use this permission" field.

### 3.1 `whatsapp_business_messaging`

> We use `whatsapp_business_messaging` to send and receive WhatsApp messages on behalf of the merchant that owns the WhatsApp Business Account. Inbound customer messages arrive through the Cloud API webhook, are stored as conversation records scoped to that merchant's tenant, and are shown in the merchant's shared inbox. Outbound messages are of three kinds: (1) free-form replies inside the 24-hour customer service window; (2) approved template messages outside that window, sent only when the merchant has a recorded opt-in and a lawful basis; and (3) order-status notifications triggered by the merchant's own order events. We never message end users on our own behalf, never send marketing to a contact list we built ourselves, and enforce a suppression list plus consent records before any out-of-window template send. Throughput is throttled per phone number with a dual token bucket, so a single tenant cannot exhaust the WABA's sending capacity.

### 3.2 `whatsapp_business_management`

> We use `whatsapp_business_management` to let a merchant onboard and administer its own WhatsApp assets from inside our product: listing the WABAs, phone numbers, and message templates the merchant has authorized; reading template review status so the merchant can correct a rejected template; and registering the phone number for Cloud API delivery. Onboarding runs through Embedded Signup v4/v5, so the merchant grants access explicitly in Meta's own UI and can revoke it at any time in Business Settings. We do not create, rename, or delete assets that the merchant did not authorize, and we do not transfer assets between Business Portfolios.

### 3.3 `pages_messaging`

> We use `pages_messaging` to read and answer Messenger conversations on Facebook Pages that the merchant administers. The merchant's inbox in our product shows the conversation next to the related order, quotation, and customer record, so an agent can answer "where is my order" with live logistics data instead of guessing. Replies are sent either by a human agent or by the tenant's AI assistant within the autonomy level the merchant configured. Outside the 24-hour standard messaging window we only send messages that Meta's policies allow; the retired tags (`CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE`, `POST_PURCHASE_UPDATE`) are not used anywhere in our codebase.

### 3.4 `instagram_manage_messages`

> We use `instagram_manage_messages` to read and answer direct messages for Instagram Professional accounts connected through Instagram API with Unified Login. The use case is the same commerce support flow as Messenger: order status, product specification questions, warranty and return requests, and B2B quotation intake. The permission is never used to send promotional broadcasts, to scrape follower lists, or to message accounts that have not contacted the merchant first.

### 3.5 `instagram_manage_comments`

> We use `instagram_manage_comments` for two purposes. First, to read comments on the merchant's own posts, Reels, and ads so that product questions ("is this IP65 rated?", "do you ship to Germany?") can be routed into the merchant's inbox as a work item. Second, to send a single private reply to a commenter within Meta's allowed window, using the private-reply endpoint with the comment ID as recipient. Private replies are one message per comment, sent only when the comment is a genuine product or order question, and the message always identifies the merchant and offers an opt-out. We do not post public comments, delete comments, or moderate on the merchant's behalf.

## 4. Screencast storyboard (one continuous recording, 3–5 minutes)

Record at 1080p with the browser address bar visible, the Meta login and permission dialog fully in frame, and no cuts inside the Embedded Signup flow. Narration may be in English; on-screen text must be English.

| Time | Screen | What the reviewer must see |
| :--- | :--- | :--- |
| 0:00–0:20 | Product login | Sign in to the merchant workspace `quanzhou-led-factory`. The tenant name and the signed-in merchant user are visible. |
| 0:20–0:45 | Settings → Channels → WhatsApp → Connect | Click **Connect WhatsApp**. Show that the button opens Meta's Embedded Signup dialog, not a self-built form. |
| 0:45–1:30 | Embedded Signup v4/v5 dialog | Log in with the merchant's Facebook account, select the Business Portfolio, create or select the WABA, verify the business phone number by SMS/voice code, and grant the requested permissions. Keep the consent screen visible for at least 5 seconds. |
| 1:30–1:50 | Coexistence branch | On the screen that offers to keep using the WhatsApp Business app, choose **keep the WhatsApp Business app** to demonstrate Coexistence onboarding. Show the message stating that chat history synchronization is starting. |
| 1:50–2:10 | Callback and asset list | Back in the product, show the connected number, its WABA ID, quality rating, and the synced conversation count. Show the audit-log entry recording who connected the channel and when. |
| 2:10–2:40 | Inbound message | From a second phone, send "Where is my order ORD-2026-1001?" to the business number. Show the message arriving in the shared inbox with the customer's local time and the linked order. |
| 2:40–3:10 | AI reply with policy gate | Trigger the AI assistant. Show the draft containing the order number, tracking number, carrier, and status pulled from the ERP tool call, and show the tool-call reference next to the draft. Approve and send; the message appears on the second phone. |
| 3:10–3:35 | L0 lock | Ask "Confirm the payment settlement of 2130 USD". Show that the assistant produces a **pending human approval** card instead of an answer, and that the send button is disabled for the AI. |
| 3:35–3:55 | Compliance filter | On a marketplace channel, attempt a reply containing a WhatsApp number. Show the outbound filter blocking the message with the reason "off-platform contact on marketplace channel". |
| 3:55–4:15 | Messenger and Instagram | Connect a Page and an Instagram Professional account, receive one Messenger message and one Instagram DM, and answer from the same inbox. Show that the channel badge differs per conversation. |
| 4:15–4:35 | Revocation | In Meta Business Settings, remove the app's access to the WABA. Back in the product, show the channel switching to `revoked` and the token being marked unusable. |
| 4:35–4:50 | Data deletion | Open Settings → Data & Privacy and show the delete-workspace action and the deletion-instructions page reachable from the app dashboard. |

## 5. WhatsApp Coexistence statement (`20 mps`, 180-day history)

> Our integration supports Coexistence onboarding for merchants that want to keep using the WhatsApp Business app on the same phone number. We implement it exactly as documented: the merchant onboards through Embedded Signup v4/v5 with session logging, our webhook endpoint accepts history notifications, and we call the SMB App Data API to start history synchronization within 24 hours of onboarding (in practice immediately). If synchronization cannot finish inside 24 hours, we offboard the number and ask the merchant to run the flow again, as Meta requires.
>
> Throughput for a Coexistence number is capped at 20 messages per second. Our sender therefore applies a per-phone-number token bucket of 20 mps for Coexistence numbers and 80 mps for Cloud API-only numbers, plus a 1-message-per-6-seconds pair limit per recipient. On `131056` (pair rate limit) and `130429` (throughput limit) we back off exponentially with a `4^X` schedule and re-queue rather than dropping the message; webhook ingestion is decoupled from sending through a Redis Stream, so a throttle never loses inbound messages.
>
> History handling follows the documented phases — phase 0 (day 0–1), phase 1 (day 1–90), phase 2 (day 90–180) — with `chunk_order`, `phase`, and `progress` used to order and monitor chunks, since chunks may arrive out of order. Group chats are excluded by Meta and are therefore excluded from our store as well. Media history contains asset IDs only for media messages sent within 14 days of onboarding; older media references are stored as text placeholders rather than fetched. A history webhook with error code `2593109` means the merchant declined to share history, and we record that decision instead of retrying.
>
> Coexistence data is stored in the merchant's own tenant only. Chat history is written to `conversations` and `messages` with the tenant ID as the row-level-security key, and the runtime database role cannot read across tenants.

## 6. `HUMAN_AGENT` seven-day compliance statement

> Instagram Messaging supports exactly one message tag for replies outside the 24-hour window: `HUMAN_AGENT`, usable up to seven days after the customer's last message. We treat that tag as a human-only escape hatch, not as an automation feature.
>
> 1. **Human-only trigger.** `HUMAN_AGENT` sends are only created from a human agent's action in the inbox. The AI runtime has no code path that can attach the tag: the outbound compliance filter rejects any message whose `message_tag` is `HUMAN_AGENT` when the sender type is not `human_agent`, and the evaluation suite asserts this block on four Messenger/Instagram cases (`D-17` to `D-20` in `hybrid_factory_eval_100.json`).
> 2. **Seven-day ceiling.** The sender checks the timestamp of the customer's last inbound message and refuses the tag when more than seven days have elapsed, even for a human agent.
> 3. **No marketing use.** The tag is used only to answer a question the customer already asked (supply-chain verification, warranty decision, order investigation). It is never used for promotions, re-engagement, or broadcast.
> 4. **Retired tags are absent.** `CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE`, and `POST_PURCHASE_UPDATE` were retired by Meta and are not referenced anywhere in the product; the outbound filter rejects them if a template ever attempts to set them.
> 5. **Visibility and audit.** Each tagged send is written to the audit log with the agent ID, the conversation ID, the tag, the elapsed time since the last inbound message, and the approval reference.

## 7. Data handling, retention, and security controls

| Control area | Implementation |
| :--- | :--- |
| Tenant isolation | PostgreSQL 17 with `ENABLE` + `FORCE ROW LEVEL SECURITY` and a `RESTRICTIVE` policy on `tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`. Runtime connections use the non-superuser role `app_runtime` through PgBouncer in `pool_mode = transaction`. A missing tenant setting returns zero rows rather than all rows. |
| Token storage | WABA, Page, and Instagram tokens are encrypted with AES-256-GCM envelope encryption; the data key is wrapped by the KMS master key and the ciphertext is bound to `AAD = "{tenant_id}:{account_id}:{cred_type}"`, so a token cannot be replayed into another tenant or account. |
| Webhook integrity | Raw-body HMAC-SHA256 verification before parsing; invalid signatures return `401` without touching the database. Verified events are persisted to `raw_webhook_events` with `ON CONFLICT DO NOTHING` for idempotency, then handed to a Redis Stream. The HTTP handler never calls an LLM or a downstream API, which keeps the `200` response inside the 100 ms budget. |
| Access control | Role-based access (owner, manager, agent, analyst) with least privilege; platform-admin access to tenant data requires a break-glass grant that is logged and time-boxed. |
| Retention | Conversation and message content is retained for the merchant's configured window and deleted on workspace deletion. Channel tokens are deleted on deauthorization. Deletion requests from data subjects are executed within 30 days and logged. |
| Transport | TLS 1.2+ for all Meta endpoints, certificate validation enabled, no pinning bypass, no plaintext fallback. |
| Logging | Application and audit logs are retained for 12 months; log records contain event metadata and IDs, not message bodies or access tokens. |
| Vulnerability management | Dependency and container scans on every build, a full scan at least every 30 days, an annual third-party penetration test, and a 7-day remediation SLA for critical findings and 30 days for high findings. |
| Incident response | A documented IR plan with severity levels, a named on-call owner, a 72-hour breach-notification path to affected merchants, and an annual tabletop exercise. |

## 8. Reviewer test instructions

1. Open `{{REVIEWER_APP_URL}}` and sign in with the reviewer credentials supplied in the App Review notes.
2. Select the workspace `quanzhou-led-factory`.
3. Channels → WhatsApp → **Connect** to exercise Embedded Signup with a test Business Portfolio. If the reviewer prefers not to onboard a real number, use the pre-connected sandbox channel named `WA Sandbox (review only)`, which is restricted to the reviewer's test recipient numbers.
4. Inbox → open the conversation seeded with order `ORD-2026-1001` and send "Where is my order ORD-2026-1001?". The reply must contain the order number, tracking number, carrier, and status, each annotated with the ERP tool-call reference.
5. Send "What is the IP rating of XJ-400Pro-EU?" to exercise strict retrieval with inline citations, then "Do you sell nuclear reactor coolant valves?" to exercise abstention and the `pending_human` handoff.
6. Send "Confirm the payment settlement of 2130 USD" to confirm the L0 lock: the assistant must not answer and must create a human approval card.
7. Settings → Data & Privacy → **Export workspace data** and **Delete workspace** demonstrate the data-subject and deletion paths.

## 9. Compliance attestations

- The app does not scrape Meta surfaces, does not automate personal accounts, and does not use unofficial clients or reverse-engineered endpoints.
- The app does not sell, license, or share Meta platform data with third parties, and does not use it to train models for other customers.
- The app does not send messages to users who have not contacted the merchant or opted in, and it honours opt-out keywords immediately across all channels of the same tenant.
- AI-generated outbound content is labelled in the merchant's audit trail, and no AI-authored message can be sent on a marketplace channel if it contains off-platform contact details.
- The app complies with the Meta Platform Terms and Developer Policies, and the merchant remains the data controller for its customers' conversation data.
