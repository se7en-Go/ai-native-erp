from __future__ import annotations

import uuid

import phonenumbers
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ConsentAndSuppressionService:
    """Manages immutable opt-in/opt-out consent_records and zero-delay suppression_list gates."""

    def _canonical_channel(self, channel_type: str) -> str:
        lower = channel_type.strip().lower()
        if lower.startswith("whatsapp"):
            return "whatsapp"
        if lower.startswith("email") or lower in ("made_in_china_email", "alibaba_email"):
            return "email"
        return lower

    def normalize_address(self, channel_type: str, raw_address: str) -> str:
        cleaned = raw_address.strip()
        family = self._canonical_channel(channel_type)
        if family == "email":
            return cleaned.lower()
        if family == "whatsapp":
            if any(c.isalpha() for c in cleaned):
                return cleaned
            candidate = cleaned if cleaned.startswith("+") else f"+{cleaned}"
            try:
                parsed = phonenumbers.parse(candidate, None)
            except phonenumbers.NumberParseException:
                parsed = None
            if parsed is not None and phonenumbers.is_possible_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            digits = "".join(ch for ch in cleaned if ch.isdigit())
            return f"+{digits}" if digits else cleaned
        return cleaned

    async def record_opt_in(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_type: str,
        proof_source: str,
        scope: list[str] | None = None,
        disclosed_business_name: str | None = None,
        proof_ip: str | None = None,
        evidence_ref: str | None = None,
        normalized_address: str | None = None,
    ) -> uuid.UUID:
        import json

        rec_id = uuid.uuid4()
        canonical = self._canonical_channel(channel_type)
        await session.execute(
            text(
                "INSERT INTO consent_records ("
                "id, tenant_id, customer_id, channel_type, consent_status, opt_in_scope, "
                "disclosed_business_name, proof_source, proof_ip, evidence_ref"
                ") VALUES ("
                ":id, :tid, :cid, :ctype, 'opted_in', CAST(:scope AS jsonb), "
                ":biz, :src, CAST(:ip AS inet), :eref"
                ")"
            ),
            {
                "id": rec_id,
                "tid": tenant_id,
                "cid": customer_id,
                "ctype": canonical,
                "scope": json.dumps(scope or ["order_updates", "promotions"]),
                "biz": disclosed_business_name,
                "src": proof_source,
                "ip": proof_ip,
                "eref": evidence_ref,
            },
        )
        if normalized_address:
            norm_addr = self.normalize_address(channel_type, normalized_address)
            await session.execute(
                text(
                    "DELETE FROM suppression_list "
                    "WHERE tenant_id = :tid AND channel_type = :ctype AND normalized_address = "
                    ":addr"
                ),
                {"tid": tenant_id, "ctype": canonical, "addr": norm_addr},
            )
        return rec_id

    async def record_stop_opt_out(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_type: str,
        normalized_address: str,
        reason: str,
        source_message_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        canonical = self._canonical_channel(channel_type)
        norm_addr = self.normalize_address(channel_type, normalized_address)
        rec_id = uuid.uuid4()

        await session.execute(
            text(
                "INSERT INTO consent_records ("
                "id, tenant_id, customer_id, channel_type, consent_status, proof_source"
                ") VALUES (:id, :tid, :cid, :ctype, 'opted_out', :src)"
            ),
            {
                "id": rec_id,
                "tid": tenant_id,
                "cid": customer_id,
                "ctype": canonical,
                "src": reason,
            },
        )
        await session.execute(
            text(
                "INSERT INTO suppression_list ("
                "id, tenant_id, channel_type, normalized_address, reason, source_message_id"
                ") VALUES (:id, :tid, :ctype, :addr, :reason, :smid) "
                "ON CONFLICT (tenant_id, channel_type, normalized_address) "
                "DO UPDATE SET reason = EXCLUDED.reason, source_message_id = "
                "EXCLUDED.source_message_id"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "ctype": canonical,
                "addr": norm_addr,
                "reason": reason,
                "smid": source_message_id,
            },
        )
        return rec_id

    async def is_suppressed(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_type: str,
        normalized_address: str,
    ) -> bool:
        canonical = self._canonical_channel(channel_type)
        norm_addr = self.normalize_address(channel_type, normalized_address)
        found = (
            await session.execute(
                text(
                    "SELECT 1 FROM suppression_list "
                    "WHERE tenant_id = :tid AND channel_type IN (:c1, :c2) "
                    "AND normalized_address = :addr LIMIT 1"
                ),
                {
                    "tid": tenant_id,
                    "c1": canonical,
                    "c2": channel_type,
                    "addr": norm_addr,
                },
            )
        ).scalar_one_or_none()
        return found is not None

    async def has_valid_opt_in(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_type: str,
    ) -> bool:
        canonical = self._canonical_channel(channel_type)
        latest = (
            await session.execute(
                text(
                    "SELECT consent_status FROM consent_records "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND channel_type IN (:c1, :c2) "
                    "ORDER BY recorded_at DESC LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "c1": canonical, "c2": channel_type},
            )
        ).scalar_one_or_none()
        return latest == "opted_in"
