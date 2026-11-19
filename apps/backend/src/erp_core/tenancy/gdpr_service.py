from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.schemas import (
    GdprAnonymizeResult,
    GdprExportResult,
    GdprTenantRedactResult,
)


class GdprService:
    """GDPR compliance workflows: customer export, PII anonymization, and tenant crypto-shred."""

    @staticmethod
    async def _table_exists(session: AsyncSession, table_name: str) -> bool:
        reg = await session.scalar(
            text("SELECT to_regclass(:name)"),
            {"name": f"public.{table_name}"},
        )
        return reg is not None

    @staticmethod
    async def _column_exists(session: AsyncSession, table_name: str, column_name: str) -> bool:
        """Schema-agnostic column probe.

        `customers.primary_phone` is the Plan 01 probe shape while the authoritative
        Plan 02 schema names the same attribute `primary_phone_e164`. The anonymizer must
        scrub whichever one the deployed schema actually has.
        """
        found = await session.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND column_name = :column"
            ),
            {"table": table_name, "column": column_name},
        )
        return found is not None

    @staticmethod
    def _serialize_row(row: Any) -> dict[str, Any]:
        return {key: value for key, value in dict(row._mapping).items()}

    async def export_customer_data(
        self,
        session: AsyncSession,
        storage: ObjectStorageService,
        tenant_id: UUID,
        customer_id: UUID,
        actor_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GdprExportResult:
        tables_payload: dict[str, list[dict[str, Any]]] = {}

        if await self._table_exists(session, "customers"):
            rows = (
                await session.execute(
                    text("SELECT * FROM customers WHERE tenant_id = :tid AND id = :cid"),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["customers"] = [self._serialize_row(row) for row in rows]

        if await self._table_exists(session, "customer_identities"):
            rows = (
                await session.execute(
                    text(
                        "SELECT * FROM customer_identities "
                        "WHERE tenant_id = :tid AND customer_id = :cid"
                    ),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["customer_identities"] = [self._serialize_row(row) for row in rows]

        if await self._table_exists(session, "conversations"):
            rows = (
                await session.execute(
                    text(
                        "SELECT * FROM conversations WHERE tenant_id = :tid AND customer_id = :cid"
                    ),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["conversations"] = [self._serialize_row(row) for row in rows]

        if await self._table_exists(session, "messages") and await self._table_exists(
            session, "conversations"
        ):
            rows = (
                await session.execute(
                    text(
                        "SELECT m.* FROM messages m "
                        "JOIN conversations c ON c.id = m.conversation_id "
                        "WHERE m.tenant_id = :tid AND c.customer_id = :cid"
                    ),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["messages"] = [self._serialize_row(row) for row in rows]

        if await self._table_exists(session, "quotes"):
            # `quotes` links to a customer directly in the Plan 01 probe shape, but the
            # authoritative Plan 03 schema reaches the customer through `opportunities`.
            if await self._column_exists(session, "quotes", "customer_id"):
                quotes_sql = "SELECT * FROM quotes WHERE tenant_id = :tid AND customer_id = :cid"
            else:
                quotes_sql = (
                    "SELECT q.* FROM quotes q "
                    "JOIN opportunities o ON o.id = q.opportunity_id "
                    "WHERE q.tenant_id = :tid AND o.customer_id = :cid"
                )
            rows = (
                await session.execute(
                    text(quotes_sql),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["quotes"] = [self._serialize_row(row) for row in rows]

        if await self._table_exists(session, "orders"):
            rows = (
                await session.execute(
                    text("SELECT * FROM orders WHERE tenant_id = :tid AND customer_id = :cid"),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["orders"] = [self._serialize_row(row) for row in rows]

        audit_rows = (
            await session.execute(
                text(
                    "SELECT id, created_at, actor_type, actor_id, action, "
                    "resource_type, resource_id "
                    "FROM audit_events "
                    "WHERE tenant_id = :tid AND resource_id = :cid "
                    "ORDER BY created_at DESC"
                ),
                {"tid": str(tenant_id), "cid": str(customer_id)},
            )
        ).all()
        tables_payload["audit_events"] = [self._serialize_row(row) for row in audit_rows]

        export_id = uuid4()
        bundle = {
            "export_id": str(export_id),
            "tenant_id": str(tenant_id),
            "customer_id": str(customer_id),
            "exported_at": datetime.now(UTC).isoformat(),
            "tables": tables_payload,
        }
        object_key = f"tenants/{tenant_id}/gdpr/exports/{customer_id}/{export_id}.json"
        await storage.ensure_bucket_exists()
        await storage.upload_bytes(
            object_key,
            json.dumps(bundle, default=str, indent=2).encode("utf-8"),
            content_type="application/json",
        )
        expires_in = 604800
        download_url = await storage.generate_presigned_get_url(object_key, expires_in=expires_in)

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "gdpr.customer_exported",
            "customer",
            customer_id,
            after_state={"export_id": str(export_id), "object_key": object_key},
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return GdprExportResult(
            export_id=export_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            object_key=object_key,
            download_url=download_url,
            expires_in_seconds=expires_in,
            tables_included=sorted(tables_payload.keys()),
        )

    async def anonymize_customer_pii(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        customer_id: UUID,
        actor_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GdprAnonymizeResult:
        digest = hashlib.sha256(f"{tenant_id}:{customer_id}".encode()).hexdigest()[:16]
        redacted_identifier = f"REDACTED_{digest}"
        redacted_email = f"{redacted_identifier}@redacted.invalid"
        identities_deleted = 0

        if await self._table_exists(session, "customers"):
            phone_column = (
                "primary_phone_e164"
                if await self._column_exists(session, "customers", "primary_phone_e164")
                else "primary_phone"
            )
            await session.execute(
                text(
                    "UPDATE customers "
                    "SET display_name = :redacted, "
                    "    primary_email = :redacted_email, "
                    f"    {phone_column} = NULL, "
                    "    updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :cid"
                ),
                {
                    "redacted": redacted_identifier,
                    "redacted_email": redacted_email,
                    "tid": str(tenant_id),
                    "cid": str(customer_id),
                },
            )

        if await self._table_exists(session, "customer_identities"):
            result = await session.execute(
                text(
                    "DELETE FROM customer_identities WHERE tenant_id = :tid AND customer_id = :cid"
                ),
                {"tid": str(tenant_id), "cid": str(customer_id)},
            )
            if isinstance(result, CursorResult):
                identities_deleted = int(result.rowcount or 0)

        if await self._table_exists(session, "messages") and await self._table_exists(
            session, "conversations"
        ):
            # Plan 02 named these `body_sanitized_html` / `attachments`; the Plan 01 probe
            # schema used `body_html` / `attachments_json`. Scrub whichever exists.
            html_column = (
                "body_sanitized_html"
                if await self._column_exists(session, "messages", "body_sanitized_html")
                else "body_html"
            )
            attachments_column = (
                "attachments"
                if await self._column_exists(session, "messages", "attachments")
                else "attachments_json"
            )
            await session.execute(
                text(
                    "UPDATE messages "
                    "SET body_text = '[REDACTED]', "
                    f"    {html_column} = NULL, "
                    f"    {attachments_column} = '[]'::jsonb "
                    "WHERE tenant_id = :tid "
                    "  AND conversation_id IN ("
                    "      SELECT id FROM conversations "
                    "      WHERE tenant_id = :tid AND customer_id = :cid"
                    "  )"
                ),
                {"tid": str(tenant_id), "cid": str(customer_id)},
            )

        preserved_tables = ["orders", "order_profit_snapshots"]
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "gdpr.customer_anonymized",
            "customer",
            customer_id,
            after_state={
                "redacted_identifier": redacted_identifier,
                "identities_deleted": identities_deleted,
                "preserved_financial_tables": preserved_tables,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return GdprAnonymizeResult(
            tenant_id=tenant_id,
            customer_id=customer_id,
            redacted_identifier=redacted_identifier,
            identities_deleted=identities_deleted,
            financial_ledger_preserved=True,
        )

    async def redact_tenant_crypto_shred(
        self,
        session: AsyncSession,
        storage: ObjectStorageService,
        crypto_service: EnvelopeCryptoService,
        tenant_id: UUID,
        actor_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GdprTenantRedactResult:
        await crypto_service.crypto_shred_tenant(session, tenant_id)
        deleted_objects = await storage.delete_prefix(f"tenants/{tenant_id}/")
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "gdpr.tenant_crypto_shredded",
            "tenant",
            tenant_id,
            after_state={"deleted_s3_objects": deleted_objects, "shredded": True},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return GdprTenantRedactResult(
            tenant_id=tenant_id,
            shredded=True,
            deleted_s3_objects=deleted_objects,
        )
