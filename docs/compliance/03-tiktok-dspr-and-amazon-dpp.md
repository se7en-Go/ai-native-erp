# TikTok Shop DSPR & Amazon SP-API Data Protection Package

**Status:** Day-1 external review package for the TikTok Shop Partner Center **Data Security and Privacy Review (DSPR)** and the Amazon Selling Partner API **Developer Profile** + **Data Protection Policy (DPP)** assessment.
**Product:** `ai-native-erp` — multi-tenant B2C + B2B commerce workspace with an L0–L2 gated AI runtime.
**Official references used:** [TikTok Shop DSPR](https://partner.tiktokshop.com/docv2/page/data-security-and-privacy-review), [TikTok Shop US Data Security Review](https://partner.tiktokshop.com/docv2/page/us-data-security-review), [TikTok Shop security requirements](https://partner.tiktokshop.com/docv2/page/13nwjzub), [Amazon SP-API security and compliance overview](https://developer-docs.amazon.com/sp-api/docs/security-compliance-overview), [Amazon vulnerability management](https://developer-docs.amazon.com/sp-api/docs/vulnerability-management), [Amazon Data Protection Policy](https://developer-docs.amazon.com/sp-api/docs/policies-and-agreements), [Register as a Public SP-API Developer](https://developer-docs.amazon.com/sp-api/docs/register-as-a-public-developer).

> **Review-scope warning:** DSPR and the US Data Security Review are separate processes with different owners, forms, and timelines. This package answers the DSPR questionnaire; the US Data Security Review is submitted only when requested in Partner Center or by the review team, and its answers are derived from the same control inventory below.

## 1. Applicant facts

| Field | Value |
| :--- | :--- |
| Legal entity | `{{LEGAL_ENTITY_NAME}}` |
| Registered address | `{{REGISTERED_ADDRESS}}` |
| Headquarters | `{{HQ_COUNTRY}}` |
| Foreign-ownership disclosure | `{{FOREIGN_OWNERSHIP_PERCENT_AND_COUNTRY}}` |
| Business category | ERP / OMS + Customer Service tooling |
| Primary and alternate workforce locations | `{{PRIMARY_WORKFORCE_LOCATION}}`, `{{ALTERNATE_WORKFORCE_LOCATION}}` |
| Production hosting regions | `{{PRIMARY_REGION}}` (primary), `{{BACKUP_REGION}}` (geographically separated backup) |
| Remote-access model | Corporate SSO + hardware-key/authenticator MFA; no standing production shell access; time-boxed, logged break-glass |
| Privacy owner / DPO | `{{DPO_NAME_AND_EMAIL}}` |
| Security owner | `{{SECURITY_OWNER_EMAIL}}` |
| Public privacy notice | `{{PRIVACY_POLICY_URL}}` |
| Sub-processor register | `{{SUBPROCESSOR_LIST_URL}}` |

## 2. TikTok Shop DSPR answers (English, per requirement item)

### 2.1 Business and entity details
> We are a software company providing a multi-tenant commerce workspace for cross-border sellers. We access TikTok Shop data only through the official Partner Center APIs after a seller authorizes our app through OAuth. We do not resell platform data, do not operate seller accounts on the seller's behalf, and do not combine TikTok Shop data with data purchased from third-party brokers. The legal entity, registration number, headquarters, and ownership structure are stated in Section 1 and in the attached corporate registration documents.

### 2.2 Workforce and system locations
> Primary workforce: `{{PRIMARY_WORKFORCE_LOCATION}}`. Alternate workforce: `{{ALTERNATE_WORKFORCE_LOCATION}}`. Production systems run in `{{PRIMARY_REGION}}`, with an encrypted, geographically separated backup in `{{BACKUP_REGION}}`. Engineering access to production is remote, requires SSO with MFA, is granted per incident with a ticket, and is recorded in a session log. Support staff cannot read message bodies or seller PII without an approved, time-boxed break-glass grant.

### 2.3 Subcontractors and vendors
> We use the following categories of sub-processors: cloud infrastructure and managed PostgreSQL, object storage, Redis, transactional email, error monitoring, and third-party LLM inference. Each is bound by a data-processing agreement with confidentiality, security, breach-notification, and deletion obligations; each is listed with the data categories it handles in the public sub-processor register. No sub-processor receives TikTok Shop data for its own purposes, and no sub-processor is permitted to train general models on seller or buyer data.

### 2.4 Data inventory and data flow
> Collected: seller account and shop identifiers, products, orders, fulfilments, returns, settlement records, buyer name, buyer shipping address, buyer phone/email where the API exposes it, and customer-service conversation content for shops that enable the support feature. Purpose: display the seller's operational data in one workspace, answer buyer questions with live order data, generate seller-facing commercial documents, and reconcile settlement. Storage: PostgreSQL in `{{PRIMARY_REGION}}`, encrypted object storage for attachments. Access: tenant-scoped application users under role-based access; no cross-tenant read path exists. Transfers: only to the sub-processors listed above, under SCCs where required. Deletion: on app deauthorization, on seller request, or per the retention schedule in §2.9.

### 2.5 Privacy roles and responsibilities
> For seller operational data and buyer personal data, the seller is the controller and we are the processor. Our privacy owner is named in Section 1 with a documented escalation path to the security owner and to legal counsel. A DPO is appointed where GDPR Art. 37 or local law requires it. Role responsibilities, the incident escalation matrix, and the data-subject-request runbook are reviewed at least annually and after any material architecture change.

### 2.6 Privacy notice
> Our public privacy notice states what data we collect, why, the legal bases, the categories of recipients, the retention periods, the security measures, the international-transfer safeguards, and how to exercise data-subject rights, including the contact channel and the 30-day response commitment. The notice is published at a stable URL and is versioned; material changes are announced to sellers before they take effect.

### 2.7 Data-subject rights
> Access, correction, deletion, restriction, portability, and objection requests are accepted by email and through an in-product form. Identity is verified before action. Because the seller is the controller for buyer data, buyer requests are routed to the seller and we execute the technical action on the seller's instruction. Our service level is acknowledgement within 2 business days and completion within 30 calendar days, with an extension notice where law permits. Every request and action is written to the audit log with a ticket reference.

### 2.8 Data minimisation
> We request only the API scopes the enabled feature needs, and we request support-related scopes only for sellers who switch the support feature on. We do not request financial payout credentials, we do not request advertising data, and we do not request data categories that have no product use. Scope justification is documented per scope in the app registration. We do not build shadow copies of platform catalogues for resale or benchmarking.

### 2.9 Retention and deletion
> Product, order, and settlement records: retained for the seller's configured period, default 24 months after the order closes, then purged. Conversation content: default 24 months, seller-configurable, and deleted on workspace deletion. Raw webhook payloads: 30 days. Application logs, which contain identifiers and event metadata rather than message bodies: 12 months. On app deauthorization or seller offboarding, channel tokens are deleted immediately and tenant data is hard-deleted within 30 days; the deletion job is idempotent and produces a deletion log with row counts. Encrypted backups rotate out within 30 days. Where a statutory retention obligation applies (for example tax records), the retained minimum is isolated, access-restricted, and documented.

### 2.10 Information security policy
> A written information security policy is approved by senior leadership and reviewed at least annually. It defines the security organisation, asset classification, acceptable use, access management, change management, encryption standards, logging and monitoring, vendor management, business continuity, and incident response, and it names the policy owner. Employees complete security awareness training at onboarding and annually, with phishing simulation.

### 2.11 Network security
> Production runs in a segmented virtual network: public load balancers only terminate TLS for the API and BFF; application and database subnets are private, with security groups allowing only the required ports between tiers; the database is not reachable from the internet. Redis and object storage are private-endpoint only. Administrative access is over a bastion/VPN path with MFA. Network flow logs, WAF rules, and host-based intrusion detection alerts feed a monitored channel with an on-call rotation.

### 2.12 Endpoint protection
> Company-managed laptops and servers run endpoint protection with a centrally managed policy, full-disk encryption, screen-lock enforcement, and automatic OS patch installation. Endpoint alerts route to the security owner. Personal devices are not permitted to access production. Removable media is restricted, and any use for platform data requires AES-256 encryption and a documented exception.

### 2.13 Security baselines
> Baseline controls: unique named accounts, no shared credentials, MFA mandatory for all administrative access including cloud consoles and the database migrator role, 14-character minimum passwords with a breached-password check, screen auto-lock at 5 minutes, hardened OS images, disabled default accounts, and quarterly review of administrative accounts. Secrets live in a managed secret store and never in source control; repository secret scanning is enabled.

### 2.14 Encryption and key management
> **In transit:** TLS 1.2+ everywhere, including database connections and internal service calls; weak ciphers disabled; HSTS on public endpoints. **At rest:** AES-256-GCM for all platform personal data and credentials, which exceeds the AES-128 minimum. Channel OAuth tokens, IMAP/SMTP secrets, and BYOK provider keys use envelope encryption: a per-tenant data key is generated by the application, the ciphertext is bound to an additional authenticated data string of the form `"{tenant_id}:{account_id}:{cred_type}"`, and the data key is wrapped by the KMS master key. A ciphertext therefore cannot be decrypted under another tenant, account, or credential type even if the database row is copied. **Key lifecycle:** the KMS covers generation, secure storage, access policy, rotation, and revocation; master-key rotation is annual and the data-key wrap format carries a key version, so rotation does not require re-encrypting tenant rows. Key material is accessible only to the application's service identity, never to human operators or logs.

### 2.15 Access control
> Role-based access control with four product roles (owner, manager, agent, analyst) and least-privilege platform roles; need-to-know enforcement in the service layer; tenant isolation enforced in the database with PostgreSQL 17 `ENABLE` + `FORCE ROW LEVEL SECURITY` and a `RESTRICTIVE` policy on `tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`; the runtime connects only as the non-superuser role `app_runtime` through PgBouncer in `pool_mode = transaction`, so a pooled connection cannot leak another tenant's context. A session without a tenant setting returns zero rows (fail-closed) rather than all rows. Access is logged with actor, action, target, timestamp, and source IP, and reviewed at least annually.

### 2.16 Vulnerability management
> Dependency, container, and IaC scanning on every build and pull request; authenticated infrastructure scans at least every 30 days and after significant network or application change; code scanning before every release; a third-party penetration test every 365 days with retest of fixed findings. Remediation SLA: critical within 7 days, high within 30 days, medium within 90 days. Findings are tracked in a ticketing system with owner, due date, and fix evidence retained for audit. Scope covers all public endpoints and the multi-tenant isolation boundary, including a cross-tenant access attempt as a mandatory penetration-test scenario.

### 2.17 Incident management
> A written incident-response plan defines severity levels, the on-call rotation, the escalation matrix, containment and eradication steps, evidence preservation, customer and regulator notification, and post-incident review. Affected sellers are notified without undue delay and within 72 hours of a confirmed personal-data breach, with the facts known at that time. An incident drill is run at least annually, and every severity-1/2 incident produces a blameless post-mortem with tracked corrective actions.

### 2.18 DLP and platform-data handling
> Platform personal data is never copied to removable media, personal cloud drives, or public links. Database exports require an approved ticket, are written to encrypted storage with an expiry, and are deleted after use. Outbound message content is scanned by the compliance filter, which blocks off-platform contact details on marketplace channels so that platform conversations are not diverted. Data-loss-prevention rules alert on bulk export and unusually large query results.

## 3. Amazon SP-API submission

### 3.1 Public Developer Profile (limit 500 words; verified length in §3.2)

```text
AI Native ERP is a multi-tenant SaaS workspace for hybrid cross-border sellers: companies that sell retail on marketplaces and directly to consumers while also quoting and shipping wholesale orders to business buyers. The app connects a seller's Amazon account through SP-API and gives the seller's team one operational view of catalogue, inventory, orders, fulfilments, returns, and settlement, alongside the conversations and quotations around those orders.

The problem is fragmentation: a hybrid seller runs a marketplace storefront, an independent store, a B2B quotation process in email, and several messaging channels. Order status lives in one system, the buyer conversation in another, and the margin calculation in a spreadsheet. Staff answer routine questions by hand, quotations are inconsistent, and nobody can say which order made money. Our product joins these views under the seller's own account and keeps the marketplace as the system of record: we do not alter listings, prices, or orders unless the seller explicitly asks, and we never act on the seller's behalf outside the authorized API.

For Amazon we use SP-API to read orders, order items, fulfilment and tracking events, inventory, and returns, and to support settlement reconciliation. Buyer personally identifiable information is requested only through the Restricted Data Token flow when a specific operational task requires it, for example producing a shipping label or a customs document. We do not request PII for analytics, we do not build buyer profiles for advertising, and we do not sell or share seller or buyer data.

AI assistance is deliberately constrained. Order status, stock, prices, MOQs, and tracking come from the seller's own database through deterministic queries cited to the query that produced them; the language model never invents a number. Replies are drafted as proposals and need human approval for commitments such as quotations and payment confirmation. Every outbound message passes a filter that blocks off-platform contact details on marketplace channels.

Security is built around tenant isolation and short-lived credentials. Data is stored in PostgreSQL with row-level security enforced by the database, credentials and PII are encrypted with AES-256-GCM envelope encryption bound to tenant and account context, and access is role-based with mandatory multi-factor authentication for administrators. Buyer PII is deleted within 30 days of the operational need ending, through automated jobs with deletion logs. We scan dependencies on every build, run vulnerability scans at least every 30 days, commission an annual penetration test, and remediate critical findings within 7 days and high findings within 30 days. An incident-response plan with a 72-hour customer notification path is tested annually.

We are applying for a public developer profile so that sellers can authorize the app themselves through Seller Central. Our support team responds to seller and Amazon security requests within one business day.
```

### 3.2 Word count of the profile

Measured with `awk`/`wc -w` on the fenced block above (whitespace-delimited words, which is the stricter reading of "500 words"):

| Metric | Value |
| :--- | :--- |
| Words | **453** |
| Limit | 500 |
| Headroom | 47 |

Re-measure before pasting, because edits change the count:

```bash
sed -n '/^### 3.1/,/^### 3.2/p' docs/compliance/03-tiktok-dspr-and-amazon-dpp.md \
  | sed -n '/^```text$/,/^```$/p' | sed '1d;$d' | wc -w
```

### 3.3 Amazon DPP control matrix

| DPP requirement | Our control | Evidence to attach |
| :--- | :--- | :--- |
| Buyer PII deleted within 30 days of the operational need ending | Nightly job deletes PII columns and derived artefacts for orders whose operational window closed; deletion log records counts and timestamps; no manual step required | Job definition, last 30 runs, deletion log sample, restore test that does not resurrect PII |
| Non-PII retention capped (18 months) | Retention policy per table with automated purge; order and settlement analytics kept in aggregated, non-identifying form | Retention schedule, purge job, table inventory |
| Log retention at least 12 months | Application, audit, and access logs shipped to encrypted, append-only storage with 12-month retention and access control | Retention configuration, sample query |
| Encryption in transit and at rest | TLS 1.2+; AES-256-GCM envelope encryption with AAD binding for tokens and PII fields; KMS-managed keys with annual rotation | Architecture diagram, key policy, encryption test output |
| Least-privilege access and MFA | RBAC, no standing production access, MFA for all admin paths, quarterly access review | Access matrix, MFA policy, last review record |
| Vulnerability scans every 30 days, pentest every 365 days, code scan per release | CI dependency and container scanning; scheduled infrastructure scan; annual third-party pentest; code scan gate before release | Scan reports, pentest report and retest letter, CI configuration |
| Critical fix within 7 days, high within 30 days | Ticketing SLA with automated escalation at 50% of the window | Ticket samples with open/fix timestamps |
| Incident response with breach notification | Written IR plan, on-call rotation, 72-hour notification path, annual drill | IR plan, drill report, escalation contact list |
| Business continuity, RTO/RPO | Geographically separated encrypted backups, restore drills, documented RTO/RPO, ability to restore PII access within hours | Backup policy, last restore test, RTO/RPO statement |
| Restricted Data Token usage | RDT requested per operational task with a minimal data-element list; tokens are not persisted; RDT failures fall back to a human task instead of broadening the request | Code path reference, RDT request sample |
| No use of SP-API data for advertising or resale | Contractual and technical restriction; no advertising integration exists; data is never joined with data-broker sources | Data-flow diagram, sub-processor list, DPA |

### 3.4 30-day PII auto-deletion SOP

| Step | Owner | Timing | Action |
| :--- | :--- | :--- | :--- |
| 1 | Automated job | Daily 02:00 UTC | Select orders whose operational window closed more than 30 days ago and whose status is terminal (delivered, refunded, cancelled, or returned-and-closed). |
| 2 | Automated job | Same run | Null out or overwrite buyer name, email, phone, and address columns; delete or anonymise derived artefacts (message transcripts containing PII, AI summaries, embeddings, cached documents, object-storage attachments). |
| 3 | Automated job | Same run | For rows that must be retained for statutory accounting, replace direct identifiers with a salted tenant-scoped pseudonym and record the legal basis in the retention register. |
| 4 | Automated job | Same run | Append to `pii_deletion_log`: run ID, tenant ID, table, row count, window covered, and outcome. |
| 5 | Automated job | Same run | If any delete fails, retry three times with backoff, then raise a page to the on-call engineer; a failed run blocks the next run from silently succeeding. |
| 6 | Security owner | Monthly | Sample 20 deleted order IDs, attempt a read through the application, and confirm that no PII is returned; attach the evidence to the compliance folder. |
| 7 | Security owner | Quarterly | Restore a backup into an isolated environment and confirm that the restore plus the cleanup job converges to the same deletion state, so a restore cannot resurrect PII. |
| 8 | Privacy owner | Annually | Review the 30-day window against actual operational needs; a shorter window requires no approval, a longer window requires a documented exception. |

### 3.5 Critical-vulnerability 7-day remediation SOP

| Day | Milestone | Action |
| :--- | :--- | :--- |
| 0 | Detection and triage | Finding arrives from scanning, a pentest, a dependency advisory, or a report. The security owner assigns a severity within 4 hours and opens a ticket with the 7-day due date for critical, 30-day for high. |
| 0–1 | Containment | If exploitation is plausible, apply the compensating control first: disable the affected route or feature flag, tighten the WAF rule, rotate the exposed credential, or revoke the affected token. Containment is recorded in the ticket. |
| 1–3 | Fix | Patch the dependency or code on a dedicated branch with a regression test that fails before the fix. Peer review is mandatory, and the reviewer must be someone other than the author. |
| 3–5 | Verification | Run the full test suite, the isolation tests that assert cross-tenant reads return zero rows, and a targeted exploit attempt against the staging environment. |
| 5–6 | Deploy | Ship through the normal release gate with a rollback plan; deploy outside business hours when the change is risky. Post-deploy, re-run the scanner that found the issue. |
| 6–7 | Close and evidence | Attach the fix commit, the passing test output, the rescan result, and the deployment record to the ticket. Close only when Rescan shows zero occurrence. |
| 7 | Escalation | If the SLA is at risk, escalate to the security owner and the CTO on day 5; an unresolved critical at day 7 is reported to leadership with a written risk acceptance, not silently extended. |
| 30 / 90 | Review | Confirm high (30-day) and medium (90-day) findings are on track, and review whether the finding class needs a preventive control, such as a lint rule, a dependency policy pin, or a test. |

## 4. Reviewer evidence index

| Evidence | Location |
| :--- | :--- |
| Encryption and key-management design | `docs/superpowers/specs/2026-11-18-omnichannel-erp-deep-research-and-architecture.md`, `infra/` |
| Tenant isolation (RLS) design and tests | `apps/backend/alembic/versions/`, `apps/backend/tests/` |
| AI grounding and abstention tests | `apps/backend/scripts/generate_eval_fixture.py`, `apps/backend/tests/fixtures/hybrid_factory_eval_100.json` |
| Webhook integrity and idempotency | `apps/backend/src/erp_core/channels/` |
| Compliance filter for off-platform leakage | `apps/backend/src/erp_core/ai_engine/compliance_filter.py` |
| Penetration-test report, scan exports, DR drill | Available on request under NDA through `{{SECURITY_OWNER_EMAIL}}` |
