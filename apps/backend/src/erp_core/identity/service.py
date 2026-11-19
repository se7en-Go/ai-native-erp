from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from phonenumbers import timezone as pn_timezone
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.identity.models import (
    B2bCompany,
    Customer,
    CustomerIdentity,
    IdentityMergeEvent,
)
from erp_core.identity.schemas import (
    BuyerLocalTimeInfo,
    CustomerSummaryRead,
    MergeProposalCandidate,
)
from erp_core.shared.enums import (
    ConfidenceLevel,
    CustomerType,
    IdentityType,
    ProposalStatus,
    ProposalType,
)
from erp_core.tenancy.audit import record_audit_event

_COUNTRY_DEFAULT_TZ: dict[str, str] = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "GB": "Europe/London",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "ES": "Europe/Madrid",
    "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam",
    "AU": "Australia/Sydney",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "SG": "Asia/Singapore",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
}


class IdentityResolverService:
    """Deterministic identity resolution, L1 pg_trgm merge proposals, and reversible splits."""

    def _normalize_identity(
        self,
        identity_type: IdentityType,
        raw_value: str,
        country_hint: str | None = None,
    ) -> tuple[str, str | None, str | None, str | None]:
        """Return (normalized_value, iana_timezone, tz_source, derived_country_code)."""
        cleaned = raw_value.strip()
        if identity_type in (IdentityType.EMAIL, IdentityType.AMAZON_BUYER_EMAIL):
            return cleaned.lower(), None, None, country_hint.upper() if country_hint else None

        if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID):
            candidate = cleaned
            if not candidate.startswith("+") and candidate.isdigit() and not country_hint:
                candidate = f"+{candidate}"
            try:
                parsed = phonenumbers.parse(
                    candidate, country_hint.upper() if country_hint else None
                )
            except phonenumbers.NumberParseException:
                parsed = None
            if parsed is not None and phonenumbers.is_possible_number(parsed):
                e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                tz_list = pn_timezone.time_zones_for_number(parsed)
                iana_tz = tz_list[0] if tz_list and tz_list[0] != "Etc/Unknown" else None
                region = phonenumbers.region_code_for_number(parsed)
                return (
                    e164,
                    iana_tz,
                    "phone_prefix" if iana_tz else None,
                    region or (country_hint.upper() if country_hint else None),
                )
            digits = "".join(ch for ch in cleaned if ch.isdigit())
            fallback_e164 = f"+{digits}" if digits else cleaned
            return fallback_e164, None, None, country_hint.upper() if country_hint else None

        hint_upper = country_hint.upper() if country_hint else None
        fallback_tz = _COUNTRY_DEFAULT_TZ.get(hint_upper) if hint_upper else None
        return (
            cleaned,
            fallback_tz,
            "shipping_country" if fallback_tz else None,
            hint_upper,
        )

    async def _follow_merge_target(
        self, session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID:
        current = customer_id
        for _ in range(5):
            merged_into = (
                await session.execute(
                    select(Customer.merged_into_customer_id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.id == current,
                    )
                )
            ).scalar_one_or_none()
            if merged_into is None:
                return current
            current = merged_into
        return current

    async def _record_l1_merge_proposal(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        source_customer_id: uuid.UUID,
        target_customer_id: uuid.UUID,
        target_display_name: str,
        similarity_score: float,
    ) -> None:
        proposal_payload = {
            "proposal_type": ProposalType.IDENTITY_MERGE.value,
            "status": ProposalStatus.PENDING.value,
            "source_customer_id": str(source_customer_id),
            "target_customer_id": str(target_customer_id),
            "target_display_name": target_display_name,
            "similarity_score": round(float(similarity_score), 4),
            "reason": "pg_trgm_name_similarity_gt_0.85",
        }
        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="identity_resolver_pg_trgm",
            action="identity.merge_proposal_created",
            resource_type="customer",
            resource_id=source_customer_id,
            after_state=proposal_payload,
        )
        has_ai_proposals = (
            await session.execute(text("SELECT to_regclass('public.ai_proposals')"))
        ).scalar_one_or_none()
        if has_ai_proposals is not None:
            clamped_score = round(min(max(float(similarity_score), 0.0), 1.0), 3)
            await session.execute(
                text(
                    "INSERT INTO ai_proposals ("
                    "id, tenant_id, proposal_type, status, capability, target_entity_type, "
                    "target_entity_id, proposed_payload, confidence_score"
                    ") VALUES ("
                    ":id, :tid, :ptype, :pstatus, 'cs_support', 'customer', "
                    ":src_id, CAST(:payload AS jsonb), :score"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_id,
                    "ptype": ProposalType.IDENTITY_MERGE.value,
                    "pstatus": ProposalStatus.PENDING.value,
                    "src_id": source_customer_id,
                    "payload": json.dumps(proposal_payload),
                    "score": Decimal(str(clamped_score)),
                },
            )

    async def resolve_or_create_customer(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        identity_type: IdentityType,
        raw_value: str,
        display_name: str,
        country_hint: str | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Link an existing customer or create one. Probabilistic hits stay proposals."""
        normalized, inferred_tz, tz_source, derived_country = self._normalize_identity(
            identity_type, raw_value, country_hint
        )

        # 1. Exact identity lookup
        existing_cid = (
            await session.execute(
                select(CustomerIdentity.customer_id).where(
                    CustomerIdentity.tenant_id == tenant_id,
                    CustomerIdentity.identity_type == identity_type.value,
                    CustomerIdentity.identity_value == normalized,
                    CustomerIdentity.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existing_cid is not None:
            resolved = await self._follow_merge_target(session, tenant_id, existing_cid)
            return resolved, False

        # 2. Cross-type deterministic link for phone_e164 <-> whatsapp_waid
        if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID):
            counterpart = (
                IdentityType.WHATSAPP_WAID
                if identity_type == IdentityType.PHONE_E164
                else IdentityType.PHONE_E164
            )
            linked_cid = (
                await session.execute(
                    select(CustomerIdentity.customer_id).where(
                        CustomerIdentity.tenant_id == tenant_id,
                        CustomerIdentity.identity_type == counterpart.value,
                        CustomerIdentity.identity_value == normalized,
                        CustomerIdentity.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if linked_cid is not None:
                resolved = await self._follow_merge_target(session, tenant_id, linked_cid)
                session.add(
                    CustomerIdentity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        customer_id=resolved,
                        identity_type=identity_type.value,
                        identity_value=normalized,
                        confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                        is_active=True,
                    )
                )
                await session.flush()
                return resolved, False

        # 3. Deterministic lookup on customers.primary_email / primary_phone_e164
        if identity_type in (IdentityType.EMAIL, IdentityType.AMAZON_BUYER_EMAIL):
            email_cid = (
                await session.execute(
                    select(Customer.id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.primary_email == normalized,
                        Customer.merged_into_customer_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if email_cid is not None:
                session.add(
                    CustomerIdentity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        customer_id=email_cid,
                        identity_type=identity_type.value,
                        identity_value=normalized,
                        confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                        is_active=True,
                    )
                )
                await session.flush()
                return email_cid, False

        if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID):
            phone_cid = (
                await session.execute(
                    select(Customer.id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.primary_phone_e164 == normalized,
                        Customer.merged_into_customer_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if phone_cid is not None:
                session.add(
                    CustomerIdentity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        customer_id=phone_cid,
                        identity_type=identity_type.value,
                        identity_value=normalized,
                        confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                        is_active=True,
                    )
                )
                await session.flush()
                return phone_cid, False

        # 4. Create new customer record (never auto-merge probabilistically!)
        new_cid = uuid.uuid4()
        clean_name = display_name.strip() or normalized
        new_customer = Customer(
            id=new_cid,
            tenant_id=tenant_id,
            customer_type=CustomerType.B2C_CONSUMER.value,
            display_name=clean_name,
            primary_email=normalized if identity_type == IdentityType.EMAIL else None,
            primary_phone_e164=(
                normalized
                if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID)
                else None
            ),
            country_code=derived_country[:2] if derived_country else None,
            preferred_language="en",
            iana_timezone=inferred_tz,
            timezone_source=tz_source,
            lifecycle_stage="lead",
            ltv_usd=Decimal("0.00"),
        )
        session.add(new_customer)
        session.add(
            CustomerIdentity(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                customer_id=new_cid,
                identity_type=identity_type.value,
                identity_value=normalized,
                confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                is_active=True,
            )
        )
        await session.flush()

        # 5. Probabilistic pg_trgm similarity > 0.85 -> L1 merge proposal only
        if len(clean_name) >= 3:
            candidates = (
                (
                    await session.execute(
                        text(
                            "SELECT id, display_name, similarity(display_name, :name) AS sim "
                            "FROM customers "
                            "WHERE tenant_id = :tid AND id != :cid "
                            "AND merged_into_customer_id IS NULL "
                            "AND display_name % :name "
                            "AND similarity(display_name, :name) > 0.85 "
                            "ORDER BY sim DESC LIMIT 3"
                        ),
                        {"tid": tenant_id, "cid": new_cid, "name": clean_name},
                    )
                )
                .mappings()
                .all()
            )
            for cand in candidates:
                await self._record_l1_merge_proposal(
                    session=session,
                    tenant_id=tenant_id,
                    source_customer_id=new_cid,
                    target_customer_id=cand["id"],
                    target_display_name=cand["display_name"],
                    similarity_score=float(cand["sim"]),
                )

        return new_cid, True

    async def list_merge_proposals(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID | None = None,
    ) -> list[MergeProposalCandidate]:
        """Return recorded L1 pg_trgm merge suggestions for human review."""
        if customer_id is not None:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT after_state, created_at FROM audit_events "
                            "WHERE tenant_id = :tid AND action = 'identity.merge_proposal_created' "
                            "AND resource_id = :cid ORDER BY created_at DESC LIMIT 20"
                        ),
                        {"tid": tenant_id, "cid": customer_id},
                    )
                )
                .mappings()
                .all()
            )
        else:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT after_state, created_at FROM audit_events "
                            "WHERE tenant_id = :tid AND action = 'identity.merge_proposal_created' "
                            "ORDER BY created_at DESC LIMIT 50"
                        ),
                        {"tid": tenant_id},
                    )
                )
                .mappings()
                .all()
            )

        results: list[MergeProposalCandidate] = []
        for row in rows:
            state = row["after_state"] or {}
            results.append(
                MergeProposalCandidate(
                    proposal_type=ProposalType(
                        state.get("proposal_type", ProposalType.IDENTITY_MERGE.value)
                    ),
                    source_customer_id=uuid.UUID(state["source_customer_id"]),
                    target_customer_id=uuid.UUID(state["target_customer_id"]),
                    target_display_name=str(state.get("target_display_name", "")),
                    similarity_score=float(state.get("similarity_score", 0.0)),
                    reason=str(state.get("reason", "pg_trgm_name_similarity_gt_0.85")),
                    created_at=row["created_at"],
                )
            )
        return results

    async def merge_customers(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        source_customer_id: uuid.UUID,
        target_customer_id: uuid.UUID,
        performed_by: str,
        reason: str,
    ) -> uuid.UUID:
        """Atomically merge source_customer into target_customer with full split lineage."""
        if source_customer_id == target_customer_id:
            raise ValueError("source_customer_id and target_customer_id must differ")

        ordered_ids = sorted([source_customer_id, target_customer_id], key=str)
        locked_rows = (
            (
                await session.execute(
                    select(Customer)
                    .where(Customer.tenant_id == tenant_id, Customer.id.in_(ordered_ids))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        by_id = {c.id: c for c in locked_rows}
        source = by_id.get(source_customer_id)
        target = by_id.get(target_customer_id)
        if source is None or target is None:
            raise LookupError("Source or target customer not found")
        if source.merged_into_customer_id is not None:
            raise ValueError("Source customer is already merged")
        if target.merged_into_customer_id is not None:
            raise ValueError("Target customer is already merged into another customer")

        moved_identity_ids = list(
            (
                await session.execute(
                    select(CustomerIdentity.id).where(
                        CustomerIdentity.tenant_id == tenant_id,
                        CustomerIdentity.customer_id == source_customer_id,
                    )
                )
            ).scalars()
        )
        if moved_identity_ids:
            await session.execute(
                text(
                    "UPDATE customer_identities SET customer_id = :target "
                    "WHERE tenant_id = :tid AND id = ANY(:ids)"
                ),
                {"tid": tenant_id, "target": target_customer_id, "ids": moved_identity_ids},
            )

        moved_conv_ids = list(
            (
                await session.execute(
                    text(
                        "UPDATE conversations SET customer_id = :target, updated_at = now() "
                        "WHERE tenant_id = :tid AND customer_id = :source RETURNING id"
                    ),
                    {"tid": tenant_id, "target": target_customer_id, "source": source_customer_id},
                )
            ).scalars()
        )
        await session.execute(
            text(
                "UPDATE messages SET customer_id = :target "
                "WHERE tenant_id = :tid AND customer_id = :source"
            ),
            {"tid": tenant_id, "target": target_customer_id, "source": source_customer_id},
        )

        source.merged_into_customer_id = target_customer_id
        if target.primary_email is None and source.primary_email is not None:
            target.primary_email = source.primary_email
        if target.primary_phone_e164 is None and source.primary_phone_e164 is not None:
            target.primary_phone_e164 = source.primary_phone_e164
        if target.iana_timezone is None and source.iana_timezone is not None:
            target.iana_timezone = source.iana_timezone
            target.timezone_source = source.timezone_source
        target.ltv_usd = (target.ltv_usd or Decimal("0.00")) + (source.ltv_usd or Decimal("0.00"))

        merge_event = IdentityMergeEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source_customer_id=source_customer_id,
            target_customer_id=target_customer_id,
            moved_identity_ids=moved_identity_ids,
            moved_conversation_ids=moved_conv_ids,
            action="merge",
            performed_by=performed_by,
            reason=reason,
        )
        session.add(merge_event)
        await session.flush()

        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=performed_by,
            action="identity.merged",
            resource_type="customer",
            resource_id=target_customer_id,
            after_state={
                "merge_event_id": str(merge_event.id),
                "source_customer_id": str(source_customer_id),
                "moved_identity_count": len(moved_identity_ids),
                "moved_conversation_count": len(moved_conv_ids),
            },
        )
        return merge_event.id

    async def split_customer_identity(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        merge_event_id: uuid.UUID,
        performed_by: str,
        reason: str,
    ) -> uuid.UUID:
        """Losslessly reverse an earlier merge event, restoring original customer ownership."""
        merge_event = (
            await session.execute(
                select(IdentityMergeEvent).where(
                    IdentityMergeEvent.tenant_id == tenant_id,
                    IdentityMergeEvent.id == merge_event_id,
                    IdentityMergeEvent.action == "merge",
                )
            )
        ).scalar_one_or_none()
        if merge_event is None:
            raise LookupError("Merge event not found")

        already_reverted = (
            await session.execute(
                select(IdentityMergeEvent.id).where(
                    IdentityMergeEvent.tenant_id == tenant_id,
                    IdentityMergeEvent.reverted_merge_event_id == merge_event_id,
                    IdentityMergeEvent.action == "split",
                )
            )
        ).scalar_one_or_none()
        if already_reverted is not None:
            raise ValueError("Merge event has already been split")

        ordered_ids = sorted(
            [merge_event.source_customer_id, merge_event.target_customer_id], key=str
        )
        locked_rows = (
            (
                await session.execute(
                    select(Customer)
                    .where(Customer.tenant_id == tenant_id, Customer.id.in_(ordered_ids))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        by_id = {c.id: c for c in locked_rows}
        source = by_id.get(merge_event.source_customer_id)
        if source is None:
            raise LookupError("Original source customer missing")

        source.merged_into_customer_id = None
        if merge_event.moved_identity_ids:
            await session.execute(
                text(
                    "UPDATE customer_identities SET customer_id = :source "
                    "WHERE tenant_id = :tid AND id = ANY(:ids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "ids": merge_event.moved_identity_ids,
                },
            )
            await session.execute(
                text(
                    "UPDATE messages SET customer_id = :source "
                    "WHERE tenant_id = :tid AND customer_identity_id = ANY(:ids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "ids": merge_event.moved_identity_ids,
                },
            )

        if merge_event.moved_conversation_ids:
            await session.execute(
                text(
                    "UPDATE conversations SET customer_id = :source, updated_at = now() "
                    "WHERE tenant_id = :tid AND id = ANY(:cids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "cids": merge_event.moved_conversation_ids,
                },
            )
            await session.execute(
                text(
                    "UPDATE messages SET customer_id = :source "
                    "WHERE tenant_id = :tid AND conversation_id = ANY(:cids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "cids": merge_event.moved_conversation_ids,
                },
            )

        split_event = IdentityMergeEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source_customer_id=merge_event.source_customer_id,
            target_customer_id=merge_event.target_customer_id,
            moved_identity_ids=list(merge_event.moved_identity_ids),
            moved_conversation_ids=list(merge_event.moved_conversation_ids),
            reverted_merge_event_id=merge_event.id,
            action="split",
            performed_by=performed_by,
            reason=reason,
        )
        session.add(split_event)
        await session.flush()

        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=performed_by,
            action="identity.split",
            resource_type="customer",
            resource_id=merge_event.source_customer_id,
            after_state={
                "split_event_id": str(split_event.id),
                "reverted_merge_event_id": str(merge_event.id),
            },
        )
        return split_event.id

    def compute_buyer_local_time(
        self, iana_timezone: str | None, now_utc: datetime | None = None
    ) -> BuyerLocalTimeInfo:
        """Compute local time, quiet hours 21:00-08:00, and the next 09:30 slot."""
        if not iana_timezone:
            return BuyerLocalTimeInfo(is_quiet_hours=False)
        try:
            tz = ZoneInfo(iana_timezone)
        except ZoneInfoNotFoundError:
            return BuyerLocalTimeInfo(is_quiet_hours=False)

        ref_utc = now_utc or datetime.now(UTC)
        if ref_utc.tzinfo is None:
            ref_utc = ref_utc.replace(tzinfo=UTC)
        local_dt = ref_utc.astimezone(tz)
        is_quiet = local_dt.hour >= 21 or local_dt.hour < 8

        recommended_utc: datetime | None = None
        if is_quiet:
            if local_dt.hour >= 21:
                target_date = local_dt.date() + timedelta(days=1)
            else:
                target_date = local_dt.date()
            next_morning_local = datetime.combine(target_date, time(9, 30), tzinfo=tz)
            recommended_utc = next_morning_local.astimezone(UTC)

        return BuyerLocalTimeInfo(
            iana_timezone=iana_timezone,
            local_iso=local_dt.isoformat(),
            formatted_time=local_dt.strftime("%I:%M %p").lstrip("0"),
            local_hour=local_dt.hour,
            is_quiet_hours=is_quiet,
            recommended_send_at_utc=recommended_utc,
        )

    async def get_customer_summaries(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, CustomerSummaryRead]:
        """Public cross-module read API returning customer summaries + buyer_local_time."""
        if not customer_ids:
            return {}
        rows = (
            (
                await session.execute(
                    select(Customer).where(
                        Customer.tenant_id == tenant_id,
                        Customer.id.in_(customer_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        now_utc = datetime.now(UTC)
        return {
            c.id: CustomerSummaryRead(
                id=c.id,
                display_name=c.display_name,
                customer_type=CustomerType(c.customer_type),
                primary_email=c.primary_email,
                primary_phone_e164=c.primary_phone_e164,
                country_code=c.country_code,
                preferred_language=c.preferred_language,
                buyer_local_time=self.compute_buyer_local_time(c.iana_timezone, now_utc),
            )
            for c in rows
        }

    async def resolve_or_create_b2b_company(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        legal_name: str,
        country_code: str = "US",
        website_domain: str | None = None,
    ) -> uuid.UUID:
        """Create or link a B2B company entity and promote the contact customer type."""
        clean_name = legal_name.strip()
        existing = (
            await session.execute(
                select(B2bCompany.id).where(
                    B2bCompany.tenant_id == tenant_id,
                    B2bCompany.legal_name == clean_name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = uuid.uuid4()
            session.add(
                B2bCompany(
                    id=existing,
                    tenant_id=tenant_id,
                    legal_name=clean_name,
                    country_code=(country_code or "US")[:2].upper(),
                    website_domain=website_domain,
                )
            )
            await session.flush()

        cust = (
            await session.execute(
                select(Customer).where(Customer.tenant_id == tenant_id, Customer.id == customer_id)
            )
        ).scalar_one_or_none()
        if cust is not None:
            cust.company_id = existing
            cust.customer_type = CustomerType.B2B_CONTACT.value
            await session.flush()
        return existing
