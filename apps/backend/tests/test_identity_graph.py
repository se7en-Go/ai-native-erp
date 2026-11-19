from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.identity.router import _require_customer_editor, customer_router
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import IdentityType, ProposalType, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal


@pytest.mark.asyncio
async def test_identity_deterministic_bsuid_probabilistic_and_reversible_split() -> None:
    migrator_engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        future=True,
    )
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    service = IdentityResolverService()

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'ID Graph Co')"),
                {"id": tenant_id, "slug": f"idg-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, "
                    "status, rate_limit_profile) "
                    "VALUES (:id, :tid, 'whatsapp_cloud', :ext, 'WA', 'active', "
                    "'{\"mps\": 80}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": f"pn_{account_id.hex[:8]}"},
            )

        async with tenant_session(session_factory, tenant_id) as sess:
            # 1. Phone E.164 normalization + deterministic WAID match
            cid_phone, created_1 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.PHONE_E164,
                "(415) 555-2671",
                "John Miller",
                country_hint="US",
            )
            assert created_1 is True

            cid_waid, created_2 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.WHATSAPP_WAID,
                "14155552671",
                "John Miller WA",
                country_hint="US",
            )
            assert created_2 is False
            assert cid_waid == cid_phone

            # Verify timezone populated from US phone prefix
            tz_val = (
                await sess.execute(
                    text("SELECT iana_timezone FROM customers WHERE id = :cid"),
                    {"cid": cid_phone},
                )
            ).scalar_one()
            assert tz_val is not None and "America/" in tz_val

            # 2. Email deterministic link (case & whitespace normalized)
            cid_email_1, created_e1 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.EMAIL,
                "  Buyer@LEDImport.DE ",
                "Klaus Weber",
                country_hint="DE",
            )
            assert created_e1 is True
            cid_email_2, created_e2 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.EMAIL,
                "buyer@ledimport.de",
                "Klaus Weber",
            )
            assert created_e2 is False
            assert cid_email_2 == cid_email_1

            # 3. WhatsApp BSUID fallback (no phone number exposed)
            cid_bsuid_1, created_b1 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.WHATSAPP_BSUID,
                "BSUID_9988776655",
                "Privacy Buyer",
            )
            assert created_b1 is True
            cid_bsuid_2, created_b2 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.WHATSAPP_BSUID,
                "BSUID_9988776655",
                "Privacy Buyer",
            )
            assert created_b2 is False
            assert cid_bsuid_2 == cid_bsuid_1

            # 4. Probabilistic name similarity > 0.85 MUST NOT auto-merge; creates L1 proposal
            cid_ig, created_ig = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.IG_PSID,
                "ig_psid_john_90210",
                "John Miller",
                country_hint="US",
            )
            assert created_ig is True
            assert cid_ig != cid_phone

            merged_flag = (
                await sess.execute(
                    text("SELECT merged_into_customer_id FROM customers WHERE id = :cid"),
                    {"cid": cid_ig},
                )
            ).scalar_one()
            assert merged_flag is None

            proposals = await service.list_merge_proposals(sess, tenant_id, cid_ig)
            assert len(proposals) >= 1
            assert proposals[0].proposal_type == ProposalType.IDENTITY_MERGE
            assert proposals[0].target_customer_id == cid_phone
            assert proposals[0].similarity_score > 0.85

            # Attach a conversation, merge, split, and confirm the original owner.
            conv_ig_id = uuid.uuid4()
            await sess.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, customer_id, channel_account_id, channel_type, "
                    "conversation_mode, status) "
                    "VALUES (:id, :tid, :cid, :aid, 'instagram_dm', 'b2c_support', 'open')"
                ),
                {"id": conv_ig_id, "tid": tenant_id, "cid": cid_ig, "aid": account_id},
            )

            merge_event_id = await service.merge_customers(
                sess,
                tenant_id,
                source_customer_id=cid_ig,
                target_customer_id=cid_phone,
                performed_by="agent_1",
                reason="Confirmed same buyer via order #1024",
            )

            conv_owner_after_merge = (
                await sess.execute(
                    text("SELECT customer_id FROM conversations WHERE id = :id"),
                    {"id": conv_ig_id},
                )
            ).scalar_one()
            assert conv_owner_after_merge == cid_phone

            split_event_id = await service.split_customer_identity(
                sess,
                tenant_id,
                merge_event_id=merge_event_id,
                performed_by="manager_1",
                reason="False positive merge",
            )
            assert split_event_id != merge_event_id

            conv_owner_after_split = (
                await sess.execute(
                    text("SELECT customer_id FROM conversations WHERE id = :id"),
                    {"id": conv_ig_id},
                )
            ).scalar_one()
            assert conv_owner_after_split == cid_ig

            src_merged_after_split = (
                await sess.execute(
                    text("SELECT merged_into_customer_id FROM customers WHERE id = :cid"),
                    {"cid": cid_ig},
                )
            ).scalar_one()
            assert src_merged_after_split is None

            ig_owner_after_split = (
                await sess.execute(
                    text(
                        "SELECT customer_id FROM customer_identities "
                        "WHERE tenant_id = :tid AND identity_value = 'ig_psid_john_90210'"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar_one()
            assert ig_owner_after_split == cid_ig

        # 5. Router smoke check for /api/v1/customers
        app = FastAPI()
        app.state.session_factory = session_factory
        app.include_router(customer_router)
        app.dependency_overrides[_require_customer_editor] = lambda: CurrentPrincipal(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            role=UserRole.MANAGER,
            email="mgr@factory.test",
        )
        from erp_core.identity.router import _require_customer_reader

        app.dependency_overrides[_require_customer_reader] = lambda: CurrentPrincipal(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            role=UserRole.AGENT,
            email="agent@factory.test",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            list_resp = await client.get("/api/v1/customers")
            assert list_resp.status_code == 200
            assert list_resp.json()["total"] >= 3
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM audit_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM identity_merge_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM conversations WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customer_identities WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await runtime_engine.dispose()
        await migrator_engine.dispose()


def test_compute_buyer_local_time_quiet_hours() -> None:
    service = IdentityResolverService()

    # 2026-11-19 04:00 UTC is 23:00 (11:00 PM) America/New_York (EST UTC-5) -> quiet hours!
    night_utc = datetime(2026, 11, 19, 4, 0, tzinfo=UTC)
    night_info = service.compute_buyer_local_time("America/New_York", night_utc)
    assert night_info.is_quiet_hours is True
    assert night_info.local_hour == 23
    assert night_info.recommended_send_at_utc == datetime(2026, 11, 19, 14, 30, tzinfo=UTC)

    # 2026-11-19 15:00 UTC is 10:00 AM EST -> working hours
    day_utc = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
    day_info = service.compute_buyer_local_time("America/New_York", day_utc)
    assert day_info.is_quiet_hours is False
    assert day_info.local_hour == 10
    assert day_info.recommended_send_at_utc is None

    # Missing or unknown timezone returns safe non-quiet defaults
    unknown_info = service.compute_buyer_local_time(None, day_utc)
    assert unknown_info.iana_timezone is None
    assert unknown_info.is_quiet_hours is False

    quiet_start = datetime(2026, 11, 19, 2, 0, tzinfo=UTC)
    start_info = service.compute_buyer_local_time("America/New_York", quiet_start)
    assert start_info.local_hour == 21
    assert start_info.is_quiet_hours is True

    quiet_end = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)
    end_info = service.compute_buyer_local_time("America/New_York", quiet_end)
    assert end_info.local_hour == 8
    assert end_info.is_quiet_hours is False

    dst_night = datetime(2026, 7, 19, 1, 0, tzinfo=UTC)
    dst_info = service.compute_buyer_local_time("America/New_York", dst_night)
    assert dst_info.local_hour == 21
    assert dst_info.is_quiet_hours is True
    assert dst_info.recommended_send_at_utc == datetime(2026, 7, 19, 13, 30, tzinfo=UTC)
