# Shopify Partner Submission — Protected Customer Data Level 2, GraphQL-only Architecture, and GDPR Compliance

**Status:** Day-1 external review package. The English answers in Sections 2–5 are written to be pasted into the Partner Dashboard review form and the App Store listing.
**Product:** `ai-native-erp` — a multi-tenant commerce workspace for hybrid B2C + B2B sellers.
**Official references used:** [Work with protected customer data](https://shopify.dev/docs/apps/launch/protected-customer-data), [Privacy law compliance (mandatory compliance webhooks)](https://shopify.dev/docs/apps/build/compliance/privacy-law-compliance), [Manage webhook subscriptions](https://shopify.dev/docs/apps/build/webhooks/subscribe), [Verify webhook deliveries](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries), [GraphQL Admin API](https://shopify.dev/docs/api/admin-graphql), [Shopify API limits](https://shopify.dev/docs/api/usage/limits).

## 1. Submission fields

| Field | Value |
| :--- | :--- |
| App name | AI Native ERP |
| App handle | `{{SHOPIFY_APP_HANDLE}}` |
| App type | Public app (App Store distribution), embedded |
| App URL | `{{APP_URL}}` |
| Privacy policy URL | `{{PRIVACY_POLICY_URL}}` |
| Support email | `{{SUPPORT_EMAIL}}` |
| Data protection officer | `{{DPO_NAME_AND_EMAIL}}` |
| Registered entity | `{{LEGAL_ENTITY_NAME}}`, `{{REGISTERED_ADDRESS}}` |
| Primary market | Cross-border B2C and B2B merchants (US, EU/EEA, UK, APAC) |
| Scopes requested | `read_orders`, `read_products`, `read_inventory`, `read_customers`, `read_fulfillments`, `read_all_orders` (justification in §3), `write_app_proxy` not requested |

## 2. Protected Customer Data Level 2 declaration

### 2.1 Which protected customer data the app accesses, and why

| Data category | Fields | Why it is required | Where it is used |
| :--- | :--- | :--- | :--- |
| Customer identity | `firstName`, `lastName` | Address a customer by name in support replies and print it on the commercial invoice that the merchant issues. | Inbox, order detail, export |
| Contact details | `email`, `phone` | Send transactional order and shipping updates through the merchant's own channels, and match an incoming WhatsApp/Messenger/Instagram conversation to the right order. | Inbox identity resolution, notifications |
| Shipping address | `address1`, `address2`, `city`, `province`, `country`, `zip`, `company` | Create shipping labels and customs paperwork, and calculate FOB/DDP freight for B2B quotations. | Fulfilment, customs, quotations |
| Order and fulfilment | Order lines, quantities, amounts, `fulfillmentOrders`, tracking numbers | Answer "where is my order" without human lookup, and reconcile settlement. | Inbox, AI order-logistics capability, finance |
| Customer notes and tags | `note`, `tags` | Preserve merchant-authored context such as "wholesale account, net-30". | Inbox, segmentation |

We do **not** request or store payment instrument data, `CustomerPaymentMethod` details, full checkout payloads, or Shopify Payments payouts. Financial amounts are stored as `NUMERIC`/`Decimal`, never as floating point.

### 2.2 Data minimisation statement (paste into the form)

> We request protected customer data only for the fields the merchant needs to answer a customer conversation, fulfil an order, or produce a commercial document. Concretely: we read customer name, email, phone, and shipping address because an order cannot be shipped, invoiced, or matched to an inbound WhatsApp/Instagram conversation without them. We do not request payment methods, we do not request the full customer object by default (we select explicit GraphQL fields per query), and we do not read order history beyond 60 days unless the merchant has approved the `read_all_orders` scope. Fields that Shopify marks as unapproved for our app are handled as redacted: our client tolerates `null` and error entries in the GraphQL response and never retries to force the field. Historical backfills use GraphQL Bulk Operations so that a large merchant does not consume interactive query cost.

### 2.3 Data flow (for the reviewer's data-flow field)

1. Merchant installs the app → OAuth 2.0 grant or token exchange on an embedded session → offline access token, encrypted at rest with AES-256-GCM envelope encryption bound to `AAD = "{tenant_id}:{shop_id}:shopify_access_token"`.
2. The app registers webhooks for `orders/create`, `orders/updated`, `fulfillments/update`, `customers/create`, `customers/update`, `app/uninstalled`, plus the three mandatory compliance topics.
3. Webhook delivery → raw-body HMAC verification (`X-Shopify-Hmac-SHA256` / `Shopify-Hmac-Sha256`) → `401` on mismatch → persist to `raw_webhook_events` with `ON CONFLICT DO NOTHING` on `Shopify-Webhook-Id` → enqueue to a Redis Stream → return `200` in under 100 ms.
4. The worker writes tenant-scoped rows. Row-level security is enforced by PostgreSQL (`FORCE ROW LEVEL SECURITY`, `RESTRICTIVE` policy on `tenant_id`), and the runtime role is a non-superuser.
5. The merchant reads the data in the inbox, order, and analytics views. AI capabilities read it only through deterministic SQL tools scoped to that tenant; live prices, stock, and tracking are never produced from static retrieval.

### 2.4 Retention and deletion

| Data | Retention | Deletion trigger |
| :--- | :--- | :--- |
| Order and customer records | Retained while the app is installed, then purged | `shop/redact` (Shopify sends 48 hours after uninstall) or merchant-initiated workspace deletion |
| Conversation transcripts | Merchant-configurable, default 24 months | Workspace deletion, or `customers/redact` for the specific subject |
| Raw webhook payloads | 30 days | Automatic purge job |
| Application and audit logs | 12 months, metadata only (no message bodies, no tokens) | Automatic purge job |
| Channel access tokens | Until deauthorization | Deleted immediately on `app/uninstalled` or token revocation |

Deletion is a hard delete of the tenant-scoped rows plus removal of derived artefacts (vector embeddings, cached summaries, object-storage blobs). Backups are encrypted and rotated out within 30 days.

### 2.5 Security controls (paste into the security field)

> Encryption in transit with TLS 1.2+; encryption at rest with AES-256-GCM envelope encryption for every Shopify access token, per-tenant data keys wrapped by KMS; optionally full-column encryption of customer email and phone. Tenant isolation is enforced at the database layer with PostgreSQL 17 `FORCE ROW LEVEL SECURITY`, not only in application code. Access is role-based and least-privilege, with an annual access review and logged break-glass access for support. Vulnerability scans run at least every 30 days, penetration tests annually, and dependency scanning on every build; critical findings are remediated within 7 days and high findings within 30 days. Logging retains metadata for 12 months and never records tokens or message bodies. An incident-response plan with named on-call ownership and merchant notification within 72 hours of a confirmed breach is maintained and exercised annually.

## 3. GraphQL-only architecture declaration

> The app is built exclusively on the **GraphQL Admin API** for all Shopify Admin data. No REST Admin API endpoint is called at runtime, and no REST-only SDK or shim is bundled. This is a deliberate architectural decision aligned with Shopify's requirement that new public apps submitted from 1 April 2025 onward be built on GraphQL.
>
> Implementation details: a single versioned GraphQL client pinned to a supported API version with a documented upgrade cadence; per-query field selection so that only approved protected-customer fields are requested; query cost accounting against each shop's leaky bucket (100/200/1,000/2,000 points per second by plan) with `429`-aware retry and backoff; GraphQL Bulk Operations for historical backfills and exports; webhook subscriptions managed through the app configuration file (compliance topics) and the Admin API (shop-scoped topics).
>
> Scope justification for `read_all_orders`: the app requests it only for merchants that reconcile fulfilment or finance beyond the default 60-day order window. The default installation does not request it; the merchant approves it explicitly, and the app degrades gracefully to the 60-day window when it is not granted.

## 4. Mandatory GDPR compliance webhooks

All three topics are subscribed in the app configuration, and the handler verifies the HMAC signature before reading the body. A request with an invalid `Shopify` HMAC header returns `401 Unauthorized` and is not processed. Valid requests return a `2xx` immediately and are completed asynchronously.

| Topic | What the app does | Service level |
| :--- | :--- | :--- |
| `customers/data_request` | Looks up every record tied to the `customer.id` (or the email when only an email is supplied) across orders, conversations, notes, and AI transcripts; produces a machine-readable export for the merchant, who is the controller and answers the data subject; writes a completion record to the audit log. | Acknowledged on receipt, delivered to the merchant within 30 days, in practice within 5 business days. |
| `customers/redact` | Hard-deletes or irreversibly anonymises the customer's personal data in the tenant: name, email, phone, address, conversation transcripts, AI summaries, and embeddings derived from them. Shopify sends this 5 days after the merchant's deletion request. If a legal retention obligation applies (for example an invoice retention requirement), the redaction is partially withheld, and the withholding reason plus the retained minimum are recorded. | Acknowledged on receipt, completed within 30 days, in practice within 48 hours where no legal hold applies. |
| `shop/redact` | Shopify sends this 48 hours after the merchant uninstalls. The app deletes the shop's tenant data: orders, customers, conversations, knowledge-base documents and embeddings, channel tokens, cached artifacts, and object-storage blobs, then marks the tenant as purged and keeps only the non-personal install/deletion audit record. | Acknowledged on receipt, completed within 30 days, in practice within 24 hours. |

Supporting behaviour: uninstall also fires `app/uninstalled`, which immediately revokes the stored access token so that no further reads are possible between uninstall and `shop/redact`. Deletion jobs are idempotent and re-runnable, and the audit log records the webhook ID, the topic, the affected tenant, and the row counts removed. Where a data subject request arrives through a channel other than Shopify, the same pipeline is used and logged.

## 5. Merchant-facing Privacy Policy (English, ready to publish)

> **Privacy Policy — AI Native ERP**
>
> Last updated: `{{PRIVACY_POLICY_LAST_UPDATED}}`
>
> **1. Who we are.** AI Native ERP ("we", "us") provides a commerce workspace that helps merchants manage customer conversations, orders, quotations, and logistics across channels such as Shopify, WhatsApp, Messenger, Instagram, TikTok Shop, Amazon, email, and a website chat widget. For personal data that we process on behalf of a merchant, the merchant is the data controller and we are the processor. For our own account, billing, and website data, we are the controller. Our contact for privacy matters is `{{PRIVACY_CONTACT_EMAIL}}`.
>
> **2. What we collect.** (a) Merchant account data: name, work email, phone, company, billing address, and payment references for subscription billing. (b) Store data that the merchant authorises us to read: products, inventory, orders, fulfilments, refunds, and, where the merchant grants the scope, customer name, email, phone, and shipping address. (c) Conversation data: messages exchanged with the merchant's customers on connected channels, including attachments, plus the metadata needed to route them (channel, timestamps, message IDs, delivery status). (d) Usage and diagnostic data: feature usage events, AI credit consumption, error traces, and IP address in server logs. (e) AI inputs and outputs: prompts, retrieved document fragments, generated drafts, and the human decisions recorded on those drafts.
>
> **3. Why we process it.** To provide the service the merchant subscribed to: display conversations in a shared inbox, match a conversation to an order, answer order-status and product-specification questions, draft quotations, produce shipping and customs documents, and reconcile settlement. To secure the service: authenticate users, enforce tenant isolation, detect abuse, and investigate incidents. To bill correctly: meter AI credit consumption and subscription usage. To comply with law: respond to valid legal requests and data-subject requests.
>
> **4. Legal bases (EEA/UK).** Performance of a contract for the processing needed to deliver the service; legitimate interests for security, abuse prevention, and product diagnostics; consent where the merchant's own customer has given it for marketing messages; legal obligation for tax, accounting, and lawful requests.
>
> **5. AI processing.** AI features generate drafts and, within merchant-configured limits, replies. Live prices, stock levels, MOQs, order status, and tracking events are retrieved from the merchant's own database through deterministic queries, not from generated text. AI output is filtered before sending, and categories the merchant marks as human-only (payment confirmation, contract terms, complaint escalation) cannot be sent by the AI. Where a third-party model provider is used, the request contains only the fragments needed for the task, is sent under a data-processing agreement, and is not used to train that provider's general models.
>
> **6. Sharing.** We share personal data with: infrastructure providers hosting our database, cache, queue, and object storage; email and messaging providers that deliver notifications; payment processors for subscription billing; model providers for AI inference; error-monitoring and support tooling. We publish the current list of sub-processors at `{{SUBPROCESSOR_LIST_URL}}` and give merchants notice before adding one that processes personal data. We do not sell personal data. We disclose data to authorities only where legally required, and we challenge overbroad requests.
>
> **7. International transfers.** Data may be processed outside the merchant's region, including in the EU, the United States, and Singapore. Transfers rely on Standard Contractual Clauses and, where applicable, the UK Addendum, with transfer impact assessments and supplementary technical measures such as encryption at rest and in transit.
>
> **8. Retention.** We keep personal data for as long as the merchant's workspace exists and the retention period the merchant configures. Order and invoice records may be kept longer where tax law requires. Raw webhook payloads are kept for 30 days; application logs, which contain identifiers and event metadata rather than message bodies, are kept for 12 months. When a workspace is deleted, or when Shopify instructs deletion after uninstall, tenant data is hard-deleted, and encrypted backups rotate out within 30 days.
>
> **9. Security.** TLS 1.2+ in transit; AES-256-GCM envelope encryption at rest for channel credentials and, optionally, customer contact fields; PostgreSQL row-level security with a non-superuser runtime role for tenant isolation; role-based access with least privilege and logged break-glass support access; vulnerability scanning at least monthly; annual penetration testing; audited incident response with notification to affected merchants without undue delay and within 72 hours of a confirmed breach.
>
> **10. Your rights.** Data subjects may request access, correction, deletion, restriction, portability, and objection. Because the merchant is the controller of store customer data, requests should first be directed to that merchant; we support the merchant with tooling and complete the request within 30 days. Merchant users can exercise their own rights by contacting `{{PRIVACY_CONTACT_EMAIL}}`. Complaints may be lodged with the supervisory authority of the data subject's residence, or with `{{SUPERVISORY_AUTHORITY}}`.
>
> **11. Cookies.** The application uses strictly necessary cookies for authentication and session management, plus optional analytics cookies that are set only after consent in the EEA/UK. The public marketing site uses a consent banner that blocks non-essential scripts until consent is given.
>
> **12. Children.** The service is not directed to children, and we do not knowingly collect data from anyone under 16.
>
> **13. Changes.** We will post material changes on this page, update the date above, and, where the change affects processing of merchant customer data, notify the merchant before it takes effect.
>
> **14. Contact.** Privacy questions and data-subject requests: `{{PRIVACY_CONTACT_EMAIL}}`. Postal address: `{{REGISTERED_ADDRESS}}`.

## 6. App listing compliance checklist

- [x] GraphQL Admin API only, no REST Admin call at runtime.
- [x] Mandatory compliance webhooks implemented, HMAC-verified, `401` on invalid signature.
- [x] Protected Customer Data Level 1 and Level 2 declared with field-level justification.
- [x] Privacy policy published at a stable URL, covering collection, purpose, sharing, retention, rights, and transfers.
- [x] Data-deletion instructions page reachable from the app dashboard.
- [x] Access scopes limited to the minimum; `read_all_orders` requested only when the merchant approves it.
- [x] No scraping, no browser automation of the Shopify admin, no off-platform customer contact harvesting.
- [x] Uninstall path revokes the token immediately and purges tenant data on `shop/redact`.
