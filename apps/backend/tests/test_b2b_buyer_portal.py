"""Track 2 — B2B buyer magic-link self-service portal (PG17 RLS + L0 Finance gate).

Everything here runs against the real PostgreSQL 17 cluster (`erp_test_t2`), never
SQLite: the point of the suite is that `b2b_portal_tokens` is `FORCE ROW LEVEL
SECURITY`, that a public buyer payload can never leak internal cost/margin data, and
that a buyer T/T upload is only ever an *unverified* claim until Finance verifies it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.crm.portal_models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.api_main import create_app
from erp_core.catalog.schemas import MoqTierInput, ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.crm.portal_service import (
    PORTAL_TOKEN_PREFIX,
    hash_portal_token,
)
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import LeadCreate, OpportunityCreate, QuoteCreate, QuoteItemInput
from erp_core.finance.payment_service import PaymentService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import Incoterm, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

_PI_TEMPLATE = {
    "company_legal_name": "Quanzhou Smart LED Lighting Co., Ltd.",
    "company_address": "88 Export Avenue, Quanzhou, Fujian, China",
    "contact_phone": "+86 595 8888 0000",
    "contact_email": "export@qzled.example.com",
    "website": "https://qzled.example.com",
    "tax_or_reg_no": "91350500MA2XXXXX1A",
    "bank_beneficiary_name": "Quanzhou Smart LED Lighting Co., Ltd.",
    "bank_name": "Bank of China, Quanzhou Branch",
    "bank_account_no": "4060 1234 5678 9012 345",
    "bank_swift_code": "BKCHCNBJ73A",
    "bank_address": "Quanzhou, Fujian, China",
    "default_terms_notes": "30% T/T deposit, 70% balance before shipment.",
}


class _FakeStorage:
    """Duck-typed `ObjectStorageService.upload_bytes` used to avoid a live MinIO."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def upload_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self.objects[key] = (data, content_type)
        return key


@dataclass
class TenantSeed:
    tenant_id: UUID
    user_id: UUID
    label: str
    base_currency: str
    quote_sent_id: UUID
    quote_draft_id: UUID
    quote_converted_id: UUID
    order_id: UUID
    deposit_rp_id: UUID
    balance_rp_id: UUID


def _runtime_url() -> str:
    return os.environ["DATABASE_RUNTIME_URL"]


async def _seed_tenant(factory: Factory, label: str, base_currency: str) -> TenantSeed:
    tenant_id = uuid4()
    user_id = uuid4()
    company_id = uuid4()
    customer_id = uuid4()
    variant_id: UUID
    quote_svc = QuoteService()

    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency, ai_policy_settings) "
                "VALUES (:id, :slug, :name, :ccy, CAST(:settings AS jsonb))"
            ),
            {
                "id": tenant_id,
                "slug": f"portal-{label}-{tenant_id.hex[:8]}",
                "name": "Quanzhou Smart LED Lighting Co., Ltd.",
                "ccy": base_currency,
                "settings": json.dumps({"pi_template": _PI_TEMPLATE}),
            },
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, :fn, 'hash')"
            ),
            {
                "id": user_id,
                "email": f"portal-{label}-{tenant_id.hex[:8]}@example.com",
                "fn": f"Owner {label}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": tenant_id, "uid": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO b2b_companies (id, tenant_id, legal_name, country_code, "
                "default_incoterm) VALUES (:id, :tid, :legal, 'ES', 'FOB')"
            ),
            {"id": company_id, "tid": tenant_id, "legal": f"Buyer {label} SL"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, company_id, "
                "display_name, primary_email, preferred_language) "
                "VALUES (:id, :tid, 'b2b_contact', :cid, :name, :mail, 'en')"
            ),
            {
                "id": customer_id,
                "tid": tenant_id,
                "cid": company_id,
                "name": f"Buyer Contact {label}",
                "mail": f"buyer-{label}@example.com",
            },
        )

        catalog = CatalogService()
        product = await catalog.create_product(
            session,
            tenant_id,
            actor_id=str(user_id),
            payload=ProductCreate(
                spu_code=f"SPU-{label}",
                category="led_lighting",
                hs_code="9405429000",
                origin_country="CN",
                i18n_specs={
                    "en": {"title": f"5M IP65 Smart LED Strip {label}"},
                    "zh": {"title": f"5米 IP65 智能灯带 {label}"},
                },
                certifications=["CE"],
            ),
        )
        variant = await catalog.create_variant(
            session,
            tenant_id,
            actor_id=str(user_id),
            role=UserRole.MANAGER,
            payload=VariantCreate(
                product_id=product.id,
                sku=f"LED-STRIP-{label}",
                cost_price_base=Decimal("42.0000"),
                base_currency=base_currency,
                min_margin_floor_pct=Decimal("15.00"),
                retail_price_usd=Decimal("18.50"),
                b2b_moq_tiers=[
                    MoqTierInput(min_qty=50, unit_price_usd=Decimal("12.00")),
                    MoqTierInput(min_qty=500, unit_price_usd=Decimal("8.50")),
                ],
                weight_grams=300,
            ),
        )
        variant_id = variant.id

        principal = CurrentPrincipal(
            user_id=user_id,
            tenant_id=tenant_id,
            role=UserRole.MANAGER,
            email=f"portal-{label}@example.com",
        )
        lead = await quote_svc.create_lead(
            session,
            tenant_id,
            LeadCreate(
                customer_id=customer_id,
                source_channel="made_in_china_email",
                lead_type="b2b_inquiry",
                qualification_score=80,
                extracted_requirements={"sku": f"LED-STRIP-{label}", "qty": 500},
            ),
        )
        opportunity = await quote_svc.create_opportunity(
            session,
            tenant_id,
            OpportunityCreate(
                lead_id=lead.id,
                company_id=company_id,
                customer_id=customer_id,
                title=f"{label} 500pcs LED strip FOB Xiamen",
                expected_amount=Decimal("4450.00"),
                currency="USD",
                owner_user_id=user_id,
            ),
        )

        async def _new_quote() -> UUID:
            quote = await quote_svc.create_quote(
                session,
                tenant_id,
                principal=principal,
                payload=QuoteCreate(
                    opportunity_id=opportunity.id,
                    is_proforma_invoice=True,
                    incoterm=Incoterm.FOB,
                    port_of_loading="Xiamen, China",
                    port_of_destination="Valencia, Spain",
                    payment_terms="30% T/T deposit, 70% before shipment",
                    base_currency=base_currency,
                    quote_currency="USD",
                    fx_rate_snapshot=Decimal("7.200000"),
                    fx_locked_until=datetime.now(UTC) + timedelta(days=10),
                    freight_fee=Decimal("200.00"),
                    items=[QuoteItemInput(variant_id=variant_id, quantity=500)],
                ),
            )
            return quote.id

        quote_sent_id = await _new_quote()
        await quote_svc.send_quote(session, tenant_id, principal, quote_sent_id)
        quote_draft_id = await _new_quote()
        quote_converted_id = await _new_quote()
        await quote_svc.send_quote(session, tenant_id, principal, quote_converted_id)

        order_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO orders (
                    id, tenant_id, order_no, order_mode, customer_id, quote_id, currency,
                    fx_rate_to_base, subtotal_amount, shipping_charged, discount_amount,
                    tax_collected, total_amount, fulfillment_status, payment_status,
                    channel_metadata
                ) VALUES (
                    :oid, :tid, :ono, 'b2b_wholesale', :cid, :qid, 'USD',
                    7.200000, 4250.00, 200.00, 0.00, 0.00, 4450.00,
                    'pending_payment', 'unpaid', CAST(:meta AS jsonb)
                )
                """
            ),
            {
                "oid": order_id,
                "tid": tenant_id,
                "ono": f"ORD-PORTAL-{label}",
                "cid": customer_id,
                "qid": quote_converted_id,
                "meta": json.dumps({"incoterm": "FOB"}),
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO order_items (
                    id, tenant_id, order_id, variant_id, sku_snapshot,
                    quantity, unit_price, unit_cost_base_snapshot, line_total
                ) VALUES (:id, :tid, :oid, :vid, :sku, 500, 8.5000, 42.0000, 4250.00)
                """
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "oid": order_id,
                "vid": variant_id,
                "sku": f"LED-STRIP-{label}",
            },
        )
        await session.execute(
            text(
                "UPDATE quotes SET status = 'converted', converted_order_id = :oid "
                "WHERE tenant_id = :tid AND id = :qid"
            ),
            {"oid": order_id, "tid": tenant_id, "qid": quote_converted_id},
        )
        receivables = await PaymentService().create_b2b_milestone_receivables(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            total_amount=Decimal("4450.00"),
            currency="USD",
            deposit_ratio=Decimal("0.30"),
        )
        by_label = {row.milestone_label: row for row in receivables}

        if label == "a":
            await session.execute(
                text(
                    """
                    INSERT INTO shipments (
                        id, tenant_id, order_id, tracking_provider, tracking_number,
                        carrier_code, shipment_mode, standardized_status, sub_status,
                        last_event_at, tracking_events, actual_shipping_cost_base
                    ) VALUES (
                        :id, :tid, :oid, '17track', 'DHL99887766',
                        'DHL', 'parcel', 'InTransit', 'CustomsHold',
                        now(), CAST(:events AS jsonb), 188.40
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tid": tenant_id,
                    "oid": order_id,
                    "events": json.dumps(
                        [
                            {
                                "time_iso": "2026-11-20T08:15:00+00:00",
                                "description": "Departed Xiamen sorting centre",
                                "location": "Xiamen, CN",
                                "stage": "InTransit",
                                "internal_cost_base": "188.40",
                                "carrier_invoice_ref": "SECRET-CARRIER-REF",
                            }
                        ]
                    ),
                },
            )

    return TenantSeed(
        tenant_id=tenant_id,
        user_id=user_id,
        label=label,
        base_currency=base_currency,
        quote_sent_id=quote_sent_id,
        quote_draft_id=quote_draft_id,
        quote_converted_id=quote_converted_id,
        order_id=order_id,
        deposit_rp_id=by_label["b2b_30_deposit"].id,
        balance_rp_id=by_label["b2b_70_balance"].id,
    )


async def _cleanup(factory: Factory, seeds: list[TenantSeed]) -> None:
    statements = (
        "DELETE FROM payment_records WHERE tenant_id = :tid",
        "DELETE FROM receivables_payables WHERE tenant_id = :tid",
        "DELETE FROM shipments WHERE tenant_id = :tid",
        "DELETE FROM order_items WHERE tenant_id = :tid",
        "DELETE FROM orders WHERE tenant_id = :tid",
        "DELETE FROM b2b_portal_tokens WHERE tenant_id = :tid",
        "DELETE FROM quote_items WHERE tenant_id = :tid",
        "DELETE FROM quotes WHERE tenant_id = :tid",
        "DELETE FROM opportunities WHERE tenant_id = :tid",
        "DELETE FROM leads WHERE tenant_id = :tid",
        "DELETE FROM customers WHERE tenant_id = :tid",
        "DELETE FROM b2b_companies WHERE tenant_id = :tid",
        "DELETE FROM product_variants WHERE tenant_id = :tid",
        "DELETE FROM products WHERE tenant_id = :tid",
    )
    for seed in seeds:
        async with tenant_session(factory, seed.tenant_id) as session:
            for statement in statements:
                await session.execute(text(statement), {"tid": seed.tenant_id})


def _principal_for(seed: TenantSeed, role: UserRole = UserRole.OWNER) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=seed.user_id,
        tenant_id=seed.tenant_id,
        role=role,
        email=f"portal-{seed.label}@example.com",
    )


def _build_app(
    factory: Factory,
    storage: _FakeStorage,
    holder: list[CurrentPrincipal],
):
    app = create_app()
    app.state.session_factory = factory
    app.state.storage = storage
    app.dependency_overrides[get_current_principal] = lambda: holder[0]
    return app


async def _create_link(
    client: AsyncClient, quote_id: UUID, payload: dict | None = None
) -> dict:
    response = await client.post(
        f"/api/v1/b2b/quotes/{quote_id}/portal-links", json=payload or {}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _walk_keys(payload: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(key)
            keys |= _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            keys |= _walk_keys(item)
    return keys


@pytest.mark.asyncio
async def test_portal_link_lifecycle_rls_isolation_and_buyer_accept_flow() -> None:
    engine = create_async_engine(_runtime_url())
    factory: Factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = _FakeStorage()
    seed_a: TenantSeed | None = None
    seed_b: TenantSeed | None = None
    try:
        seed_a = await _seed_tenant(factory, "a", "CNY")
        seed_b = await _seed_tenant(factory, "b", "USD")

        holder = [_principal_for(seed_a)]
        app = _build_app(factory, storage, holder)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. Operator mints a magic link for a *sent* quote.
            created = await _create_link(
                client,
                seed_a.quote_sent_id,
                {"buyer_email": "carlos@iberialight.example.com", "expires_in_days": 14},
            )
            plain_token = created["plain_token"]
            link = created["link"]
            assert plain_token.startswith(PORTAL_TOKEN_PREFIX)
            assert created["portal_path"] == f"/portal/{plain_token}"
            assert link["token_prefix"] == plain_token[:16]
            assert link["is_active"] is True
            assert link["buyer_email"] == "carlos@iberialight.example.com"
            assert "token_hash" not in link and "token_hash" not in created
            assert "plain_token" not in link

            # 2. Only token_prefix + SHA-256 hex are persisted; no plaintext anywhere.
            async with tenant_session(factory, seed_a.tenant_id) as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT token_hash, token_prefix, "
                            "concat_ws('|', id, quote_id, created_by_user_id, token_prefix, "
                            "token_hash, coalesce(buyer_email, '')) AS flat "
                            "FROM b2b_portal_tokens WHERE id = :id"
                        ),
                        {"id": link["id"]},
                    )
                ).one()
                assert row.token_hash == hashlib.sha256(plain_token.encode()).hexdigest()
                assert row.token_hash == hash_portal_token(plain_token)
                assert len(row.token_hash) == 64
                assert plain_token not in row.flat
                assert plain_token[16:] not in row.flat

                # 3. FORCE RLS is really on, and the table is a tenant table.
                flags = (
                    await session.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname = 'b2b_portal_tokens'"
                        )
                    )
                ).one()
                assert flags.relrowsecurity is True
                assert flags.relforcerowsecurity is True

            # 4. Tenant B cannot list or revoke Tenant A's links (RLS + tenant filter).
            holder[0] = _principal_for(seed_b)
            b_links = await client.get(
                f"/api/v1/b2b/quotes/{seed_a.quote_sent_id}/portal-links"
            )
            assert b_links.status_code == 200
            assert b_links.json() == []
            own_links_b = await client.get(
                f"/api/v1/b2b/quotes/{seed_b.quote_sent_id}/portal-links"
            )
            assert own_links_b.status_code == 200
            assert own_links_b.json() == []
            cross_revoke = await client.delete(f"/api/v1/b2b/portal-links/{link['id']}")
            assert cross_revoke.status_code == 404
            assert cross_revoke.json()["code"] == "ERR_PORTAL_LINK_NOT_FOUND"
            async with tenant_session(factory, seed_b.tenant_id) as session:
                visible = (
                    await session.execute(text("SELECT count(*) FROM b2b_portal_tokens"))
                ).scalar_one()
                assert visible == 0

            holder[0] = _principal_for(seed_a)
            listed = await client.get(
                f"/api/v1/b2b/quotes/{seed_a.quote_sent_id}/portal-links"
            )
            assert listed.status_code == 200
            assert [row["id"] for row in listed.json()] == [link["id"]]

            # 5. Public buyer view is authenticated by the token alone — no cookies.
            view = await client.get(f"/api/v1/portal/{plain_token}")
            assert view.status_code == 200, view.text
            body = view.json()
            assert body["seller"]["company_name"] == _PI_TEMPLATE["company_legal_name"]
            assert body["seller"]["bank_swift_code"] == "BKCHCNBJ73A"
            assert body["quote"]["quote_no"]
            assert body["quote"]["status"] == "sent"
            assert body["quote"]["incoterm"] == "FOB"
            assert body["quote"]["total_amount"] == "4450.00"
            assert body["can_accept"] is True
            assert body["can_upload_receipt"] is False
            assert body["order"] is None
            assert len(body["quote"]["items"]) == 1
            assert set(body["quote"]["items"][0]) == {
                "sku",
                "description",
                "quantity",
                "unit_price",
                "line_total",
            }

            # 6. Zero cost / margin / landed-cost keys anywhere in the public payload.
            keys = _walk_keys(body)
            for forbidden in (
                "unit_cost_base_snapshot",
                "cost_price_base",
                "net_contribution_margin_base",
                "gross_revenue_base",
                "actual_shipping_cost_base",
                "internal_cost_base",
                "carrier_invoice_ref",
                "ai_policy_settings",
                "landed_cost",
            ):
                assert forbidden not in keys, f"public payload leaked {forbidden}"
            assert "42.0000" not in json.dumps(body)

            # 7. Malformed / unknown tokens are rejected with 401.
            for bad in ("not-a-portal-token", f"{PORTAL_TOKEN_PREFIX}nope-nope-nope"):
                rejected = await client.get(f"/api/v1/portal/{bad}")
                assert rejected.status_code == 401
                assert rejected.json()["code"] == "ERR_INVALID_PORTAL_TOKEN"

            # 8. Expired token -> 401.
            expired_plain = f"{PORTAL_TOKEN_PREFIX}{uuid4().hex}"
            async with tenant_session(factory, seed_a.tenant_id) as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO b2b_portal_tokens (
                            id, tenant_id, quote_id, created_by_user_id,
                            token_prefix, token_hash, expires_at
                        ) VALUES (
                            :id, :tid, :qid, :uid, :prefix, :thash,
                            now() - interval '1 hour'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "tid": seed_a.tenant_id,
                        "qid": seed_a.quote_sent_id,
                        "uid": seed_a.user_id,
                        "prefix": expired_plain[:16],
                        "thash": hash_portal_token(expired_plain),
                    },
                )
            expired = await client.get(f"/api/v1/portal/{expired_plain}")
            assert expired.status_code == 401
            assert expired.json()["code"] == "ERR_INVALID_PORTAL_TOKEN"

            # 9. Draft quotes are not acceptable through the portal (409).
            draft_link = await _create_link(client, seed_a.quote_draft_id)
            draft_accept = await client.post(
                f"/api/v1/portal/{draft_link['plain_token']}/accept",
                json={"buyer_name": "Carlos Mendez"},
            )
            assert draft_accept.status_code == 409
            assert draft_accept.json()["code"] == "ERR_PORTAL_QUOTE_NOT_SENDABLE"

            # 10. Accepting transitions sent -> accepted, stamps the token, audits as webhook.
            accepted = await client.post(
                f"/api/v1/portal/{plain_token}/accept",
                json={"buyer_name": "Carlos Mendez"},
            )
            assert accepted.status_code == 200, accepted.text
            accepted_body = accepted.json()
            assert accepted_body["status"] == "accepted"
            assert accepted_body["accepted_by_name"] == "Carlos Mendez"
            assert accepted_body["accepted_at"] is not None

            async with tenant_session(factory, seed_a.tenant_id) as session:
                quote_row = (
                    await session.execute(
                        text("SELECT status FROM quotes WHERE tenant_id = :tid AND id = :qid"),
                        {"tid": seed_a.tenant_id, "qid": seed_a.quote_sent_id},
                    )
                ).one()
                assert quote_row.status == "accepted"
                audit = (
                    await session.execute(
                        text(
                            "SELECT actor_type, actor_id, after_state ->> 'status' AS status "
                            "FROM audit_events WHERE tenant_id = :tid "
                            "AND action = 'quote.accepted_via_portal' AND resource_id = :qid"
                        ),
                        {"tid": seed_a.tenant_id, "qid": seed_a.quote_sent_id},
                    )
                ).all()
                assert len(audit) == 1
                assert audit[0].actor_type == "webhook"
                assert audit[0].actor_id == f"portal:{link['id']}"
                assert audit[0].status == "accepted"
                stamp = (
                    await session.execute(
                        text(
                            "SELECT accepted_at, accepted_by_name, host(accepted_ip) AS ip, "
                            "last_accessed_at FROM b2b_portal_tokens WHERE id = :id"
                        ),
                        {"id": link["id"]},
                    )
                ).one()
                assert stamp.accepted_at is not None
                assert stamp.accepted_by_name == "Carlos Mendez"
                assert stamp.ip is not None
                assert stamp.last_accessed_at is not None

            # 11. Replay is idempotent: same accepted_at, no second acceptance audit.
            replay = await client.post(
                f"/api/v1/portal/{plain_token}/accept",
                json={"buyer_name": "Carlos Mendez"},
            )
            assert replay.status_code == 200
            assert replay.json()["status"] == "accepted"
            assert replay.json()["accepted_at"] == accepted_body["accepted_at"]
            async with tenant_session(factory, seed_a.tenant_id) as session:
                count = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM audit_events WHERE tenant_id = :tid "
                            "AND action = 'quote.accepted_via_portal' AND resource_id = :qid"
                        ),
                        {"tid": seed_a.tenant_id, "qid": seed_a.quote_sent_id},
                    )
                ).scalar_one()
                assert count == 1

            after_accept_view = await client.get(f"/api/v1/portal/{plain_token}")
            assert after_accept_view.status_code == 200
            assert after_accept_view.json()["quote"]["status"] == "accepted"
            assert after_accept_view.json()["can_accept"] is False

            # 12. Revoked tokens stop working immediately.
            restored = await client.get(f"/api/v1/b2b/quotes/{seed_a.quote_sent_id}/portal-links")
            assert restored.status_code == 200
            revoked = await client.delete(f"/api/v1/b2b/portal-links/{link['id']}")
            assert revoked.status_code == 200
            assert revoked.json()["revoked_at"] is not None
            assert revoked.json()["is_active"] is False
            dead = await client.get(f"/api/v1/portal/{plain_token}")
            assert dead.status_code == 401
            assert dead.json()["code"] == "ERR_INVALID_PORTAL_TOKEN"
    finally:
        seeds = [seed for seed in (seed_a, seed_b) if seed is not None]
        if seeds:
            await _cleanup(factory, seeds)
        await engine.dispose()


@pytest.mark.asyncio
async def test_buyer_receipt_upload_stays_unverified_until_finance_l0_gate() -> None:
    engine = create_async_engine(_runtime_url())
    factory: Factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = _FakeStorage()
    seed_a: TenantSeed | None = None
    seed_b: TenantSeed | None = None
    try:
        seed_a = await _seed_tenant(factory, "a", "CNY")
        seed_b = await _seed_tenant(factory, "b", "USD")
        holder = [_principal_for(seed_a)]
        app = _build_app(factory, storage, holder)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            link = await _create_link(client, seed_a.quote_converted_id)
            token = link["plain_token"]

            view = await client.get(f"/api/v1/portal/{token}")
            assert view.status_code == 200, view.text
            view_body = view.json()
            order = view_body["order"]
            assert order is not None
            assert order["order_no"] == "ORD-PORTAL-a"
            assert order["payment_status"] == "unpaid"
            assert view_body["can_upload_receipt"] is True
            milestones = {m["milestone_label"]: m for m in order["receivables"]}
            assert milestones["b2b_30_deposit"]["due_amount"] == "1335.00"
            assert milestones["b2b_70_balance"]["due_amount"] == "3115.00"
            assert all(m["paid_amount"] == "0.00" for m in order["receivables"])
            shipment = order["shipments"][0]
            assert shipment["tracking_number"] == "DHL99887766"
            assert shipment["carrier"] == "DHL"
            assert shipment["standardized_status"] == "InTransit"
            assert shipment["tracking_events"][0]["description"] == (
                "Departed Xiamen sorting centre"
            )
            assert shipment["tracking_events"][0]["location"] == "Xiamen, CN"
            assert "internal_cost_base" not in json.dumps(view_body)
            assert "SECRET-CARRIER-REF" not in json.dumps(view_body)

            # Milestone from another tenant/order is not payable through this link.
            foreign = await client.post(
                f"/api/v1/portal/{token}/receipt",
                data={
                    "rp_id": str(seed_b.deposit_rp_id),
                    "payment_method": "bank_transfer",
                    "external_tx_ref": "TT-FOREIGN-1",
                    "amount_received": "1335.00",
                    "currency": "USD",
                },
                files={"receipt_file": ("slip.png", b"\x89PNG\r\n\x1a\n slip", "image/png")},
            )
            assert foreign.status_code == 404
            assert foreign.json()["code"] == "ERR_RECEIVABLE_NOT_FOUND"

            # Wrong currency is rejected before any row is written.
            wrong_ccy = await client.post(
                f"/api/v1/portal/{token}/receipt",
                data={
                    "rp_id": str(seed_a.deposit_rp_id),
                    "payment_method": "bank_transfer",
                    "amount_received": "1335.00",
                    "currency": "EUR",
                },
            )
            assert wrong_ccy.status_code == 422
            assert wrong_ccy.json()["code"] == "ERR_PAYMENT_CURRENCY_MISMATCH"

            # Unsupported bank-slip MIME type is rejected.
            bad_type = await client.post(
                f"/api/v1/portal/{token}/receipt",
                data={
                    "rp_id": str(seed_a.deposit_rp_id),
                    "payment_method": "bank_transfer",
                    "amount_received": "1335.00",
                    "currency": "USD",
                },
                files={"receipt_file": ("slip.exe", b"MZ\x00", "application/x-msdownload")},
            )
            assert bad_type.status_code == 422
            assert bad_type.json()["code"] == "ERR_PORTAL_RECEIPT_TYPE_INVALID"

            # Oversized bank slip (> 5 MB) is rejected.
            too_big = await client.post(
                f"/api/v1/portal/{token}/receipt",
                data={
                    "rp_id": str(seed_a.deposit_rp_id),
                    "payment_method": "bank_transfer",
                    "amount_received": "1335.00",
                    "currency": "USD",
                },
                files={
                    "receipt_file": (
                        "slip.pdf",
                        b"%PDF-" + b"0" * (5 * 1024 * 1024),
                        "application/pdf",
                    )
                },
            )
            assert too_big.status_code == 422
            assert too_big.json()["code"] == "ERR_PORTAL_RECEIPT_TOO_LARGE"

            # A valid T/T slip: multipart form + PNG attachment.
            png_bytes = b"\x89PNG\r\n\x1a\n" + b"bank-slip-payload"
            upload = await client.post(
                f"/api/v1/portal/{token}/receipt",
                data={
                    "rp_id": str(seed_a.deposit_rp_id),
                    "payment_method": "wire_transfer",
                    "external_tx_ref": "TT-PORTAL-0001",
                    "amount_received": "1335.00",
                    "currency": "USD",
                },
                files={"receipt_file": ("../../etc/passwd.png", png_bytes, "image/png")},
            )
            assert upload.status_code == 201, upload.text
            upload_body = upload.json()
            assert upload_body["status"] == "pending_verification"
            assert upload_body["amount_received"] == "1335.00"
            assert upload_body["currency"] == "USD"
            receipt_key = upload_body["receipt_r2_key"]
            assert receipt_key is not None
            assert receipt_key.startswith(f"receipts/{seed_a.tenant_id}/portal/")
            # Path traversal is stripped: only the sanitized basename survives.
            assert "../" not in receipt_key and "/etc/" not in receipt_key
            assert receipt_key.rsplit("/", 1)[-1].endswith("_passwd.png")
            assert storage.objects[receipt_key] == (png_bytes, "image/png")

            # Auditor view: the row is UNVERIFIED and the AR state is untouched.
            async with tenant_session(factory, seed_a.tenant_id) as session:
                payment = (
                    await session.execute(
                        text(
                            "SELECT verified_by_user_id, receipt_r2_key, amount_received, "
                            "currency, actual_fx_rate, payment_method, external_tx_ref "
                            "FROM payment_records WHERE tenant_id = :tid AND id = :pid"
                        ),
                        {"tid": seed_a.tenant_id, "pid": upload_body["payment_id"]},
                    )
                ).one()
                assert payment.verified_by_user_id is None
                assert payment.receipt_r2_key == receipt_key
                assert payment.amount_received == Decimal("1335.00")
                assert payment.currency == "USD"
                # USD receivable against a CNY book: the quote FX snapshot is used.
                assert payment.actual_fx_rate == Decimal("7.200000")
                assert payment.payment_method == "wire_transfer"
                assert payment.external_tx_ref == "TT-PORTAL-0001"

                rp_state = (
                    await session.execute(
                        text(
                            "SELECT paid_amount, status FROM receivables_payables "
                            "WHERE tenant_id = :tid AND id = :rp"
                        ),
                        {"tid": seed_a.tenant_id, "rp": seed_a.deposit_rp_id},
                    )
                ).one()
                assert rp_state.paid_amount == Decimal("0.00")
                assert rp_state.status == "open"

                order_state = (
                    await session.execute(
                        text(
                            "SELECT payment_status FROM orders "
                            "WHERE tenant_id = :tid AND id = :oid"
                        ),
                        {"tid": seed_a.tenant_id, "oid": seed_a.order_id},
                    )
                ).one()
                assert order_state.payment_status == "unpaid"

                audit = (
                    await session.execute(
                        text(
                            "SELECT actor_type, actor_id, after_state ->> 'verified_by_user_id' "
                            "AS verified FROM audit_events WHERE tenant_id = :tid "
                            "AND action = 'payment.submitted_via_portal' "
                            "AND resource_id = :pid"
                        ),
                        {
                            "tid": seed_a.tenant_id,
                            "pid": upload_body["payment_id"],
                        },
                    )
                ).one()
                assert audit.actor_type == "webhook"
                assert audit.actor_id == f"portal:{link['link']['id']}"
                assert audit.verified is None

            # A duplicate bank reference cannot be booked twice.
            duplicate = await client.post(
                f"/api/v1/portal/{token}/receipt",
                json={
                    "rp_id": str(seed_a.deposit_rp_id),
                    "payment_method": "wire_transfer",
                    "external_tx_ref": "TT-PORTAL-0001",
                    "amount_received": "1335.00",
                    "currency": "USD",
                },
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == "ERR_DUPLICATE_PAYMENT_REF"

            # The order is still unpaid after the buyer upload; only Finance L0 settles AR.
            still_unpaid = await client.get(f"/api/v1/portal/{token}")
            assert still_unpaid.json()["order"]["payment_status"] == "unpaid"

            async with tenant_session(factory, seed_a.tenant_id) as session:
                finance = _principal_for(seed_a, UserRole.FINANCE)
                verified = await PaymentService().verify_payment(
                    session, seed_a.tenant_id, finance, UUID(upload_body["payment_id"])
                )
                assert verified.ok is True
                assert verified.payment.verified_by_user_id == seed_a.user_id
                assert verified.order_fulfillment_status.value  # order state was reloaded

            async with tenant_session(factory, seed_a.tenant_id) as session:
                settled = (
                    await session.execute(
                        text(
                            "SELECT payment_status FROM orders "
                            "WHERE tenant_id = :tid AND id = :oid"
                        ),
                        {"tid": seed_a.tenant_id, "oid": seed_a.order_id},
                    )
                ).one()
                assert settled.payment_status == "deposit_paid"
                rp_after = (
                    await session.execute(
                        text(
                            "SELECT paid_amount, status FROM receivables_payables "
                            "WHERE tenant_id = :tid AND id = :rp"
                        ),
                        {"tid": seed_a.tenant_id, "rp": seed_a.deposit_rp_id},
                    )
                ).one()
                assert rp_after.paid_amount == Decimal("1335.00")
                assert rp_after.status == "settled"

            # Tenant B (book currency = USD) settles same-currency: FX must be exactly 1.
            holder[0] = _principal_for(seed_b)
            link_b = await _create_link(client, seed_b.quote_converted_id)
            same_ccy = await client.post(
                f"/api/v1/portal/{link_b['plain_token']}/receipt",
                json={
                    "rp_id": str(seed_b.deposit_rp_id),
                    "payment_method": "bank_transfer",
                    "external_tx_ref": "TT-PORTAL-B-1",
                    "amount_received": "1335.00",
                    "currency": "USD",
                },
            )
            assert same_ccy.status_code == 201, same_ccy.text
            async with tenant_session(factory, seed_b.tenant_id) as session:
                fx = (
                    await session.execute(
                        text(
                            "SELECT actual_fx_rate, verified_by_user_id FROM payment_records "
                            "WHERE tenant_id = :tid AND id = :pid"
                        ),
                        {"tid": seed_b.tenant_id, "pid": same_ccy.json()["payment_id"]},
                    )
                ).one()
                assert fx.actual_fx_rate == Decimal("1.000000")
                assert fx.verified_by_user_id is None
    finally:
        seeds = [seed for seed in (seed_a, seed_b) if seed is not None]
        if seeds:
            await _cleanup(factory, seeds)
        await engine.dispose()


@pytest.mark.asyncio
async def test_buyer_portal_documents_render_pi_ci_pl_pdf_bytes() -> None:
    engine = create_async_engine(_runtime_url())
    factory: Factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = _FakeStorage()
    seed_a: TenantSeed | None = None
    try:
        seed_a = await _seed_tenant(factory, "a", "CNY")
        holder = [_principal_for(seed_a)]
        app = _build_app(factory, storage, holder)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            sent_link = await _create_link(client, seed_a.quote_sent_id)
            converted_link = await _create_link(client, seed_a.quote_converted_id)

            pi = await client.get(f"/api/v1/portal/{sent_link['plain_token']}/documents/pi")
            assert pi.status_code == 200, pi.text
            assert pi.headers["content-type"] == "application/pdf"
            assert pi.content.startswith(b"%PDF-")
            assert "attachment" in pi.headers["content-disposition"]
            assert ".pdf" in pi.headers["content-disposition"]

            # CI/PL are only issued after the quote became an order.
            early_ci = await client.get(
                f"/api/v1/portal/{sent_link['plain_token']}/documents/ci"
            )
            assert early_ci.status_code == 409
            assert early_ci.json()["code"] == "ERR_PORTAL_QUOTE_NOT_CONVERTED"

            for doc_type, expected_prefix in (("pi", "PI-"), ("ci", "CI-"), ("pl", "PL-")):
                response = await client.get(
                    f"/api/v1/portal/{converted_link['plain_token']}/documents/{doc_type}",
                    params={"language": "zh"},
                )
                assert response.status_code == 200, response.text
                assert response.content.startswith(b"%PDF-")
                assert len(response.content) > 1000
                assert expected_prefix in response.headers["content-disposition"]
                assert response.headers["cache-control"] == "no-store"

            # Unknown document types are a 422 contract error, not a 500.
            bad_type = await client.get(
                f"/api/v1/portal/{converted_link['plain_token']}/documents/xyz"
            )
            assert bad_type.status_code == 422

            # Documents are still token-scoped: a foreign/revoked token gets no bytes.
            revoked = await client.delete(
                f"/api/v1/b2b/portal-links/{converted_link['link']['id']}"
            )
            assert revoked.status_code == 200
            dead = await client.get(
                f"/api/v1/portal/{converted_link['plain_token']}/documents/pi"
            )
            assert dead.status_code == 401
    finally:
        if seed_a is not None:
            await _cleanup(factory, [seed_a])
        await engine.dispose()
