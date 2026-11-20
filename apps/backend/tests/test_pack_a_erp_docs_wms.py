from __future__ import annotations

import io
import uuid as uuidlib
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.security import create_access_token

CSV_HEADER = (
    "SPU_CODE,Product_Name,Category,HS_Code,SKU,Retail_Price_USD,Cost_Price_Base,"
    "Lead_Time_Days,Carton_Qty,Gross_Weight_KG,Net_Weight_KG,CBM_Per_Carton,"
    "Safety_Stock_Qty,MOQ_100_Price,MOQ_500_Price"
)


def _csv(*rows: str) -> str:
    return "\n".join((CSV_HEADER, *rows))


async def _member_headers(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    role: UserRole,
) -> dict[str, str]:
    """Create a real membership row and mint an access token for it."""
    user_id, email = uuid4(), f"{role.value}-{uuid4().hex[:8]}@factory.example.com"
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Pack A Tester', 'hash')"
            ),
            {"uid": user_id, "email": email},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, :role)"
            ),
            {"tid": tenant_id, "uid": user_id, "role": role.value},
        )
    token = create_access_token(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        email=email,
        settings=get_settings(),
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed_variant(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    spu_code: str,
    sku: str,
    title: str,
    hs_code: str = "9405429000",
    retail_price_usd: str = "10.00",
    cost_price_base: str = "4.0000",
    attributes: str | None = None,
    on_hand: int = 0,
    reserved: int = 0,
    warehouse_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    product_id, variant_id = uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category, hs_code, i18n_specs) "
                "VALUES (:pid, :tid, :spu, 'led_lighting', :hs, CAST(:specs AS jsonb))"
            ),
            {
                "pid": product_id,
                "tid": tenant_id,
                "spu": spu_code,
                "hs": hs_code,
                "specs": f'{{"en": {{"title": "{title}"}}}}',
            },
        )
        await session.execute(
            text(
                "INSERT INTO product_variants ("
                " id, tenant_id, product_id, sku, cost_price_base, retail_price_usd,"
                " weight_grams, lead_time_days, attributes"
                ") VALUES (:vid, :tid, :pid, :sku, :cost, :retail, 120, 10, "
                "CAST(:attrs AS jsonb))"
            ),
            {
                "vid": variant_id,
                "tid": tenant_id,
                "pid": product_id,
                "sku": sku,
                "cost": Decimal(cost_price_base),
                "retail": Decimal(retail_price_usd),
                "attrs": attributes or "{}",
            },
        )
        if warehouse_id is not None:
            await session.execute(
                text(
                    "INSERT INTO inventory_balances "
                    "(tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty) "
                    "VALUES (:tid, :wid, :vid, :on_hand, :reserved)"
                ),
                {
                    "tid": tenant_id,
                    "wid": warehouse_id,
                    "vid": variant_id,
                    "on_hand": on_hand,
                    "reserved": reserved,
                },
            )
    return product_id, variant_id


async def test_catalog_csv_import_upserts_products_variants_and_packing_specs(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}
    sheet = _csv(
        "QZ-STRIP-5M,5M IP65 RGBW LED Strip,led_lighting,9405429000,SKU-STRIP-5M,"
        "18.50,9.20,12,25,0.62,0.50,0.0300,60,16.80,15.40",
        "QZ-STRIP-5M,5M IP65 RGBW LED Strip,led_lighting,9405429000,SKU-STRIP-10M,"
        "32.00,16.40,14,20,1.10,0.95,0.0450,40,,",
        "QZ-DOWNLIGHT-12W,12W Anti-Glare Downlight,led_lighting,9405409000,"
        "SKU-DOWNLIGHT-12W,7.90,3.60,9,50,8.40,7.80,0.0620,80,7.10,6.60",
    )
    response = await async_client.post(
        "/api/v1/catalog/variants/import-csv", headers=headers, json={"csv_text": sheet}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "created_products": 2,
        "created_variants": 3,
        "updated_variants": 0,
        "errors": [],
    }

    variants = await async_client.get(
        "/api/v1/catalog/variants?sku=SKU-STRIP-5M", headers=headers
    )
    assert variants.status_code == 200, variants.text
    variant = variants.json()["items"][0]
    attributes = variant["attributes"]
    assert attributes["carton_qty"] == 25
    assert Decimal(str(attributes["gross_weight_kg"])) == Decimal("0.62")
    assert Decimal(str(attributes["net_weight_kg"])) == Decimal("0.50")
    assert Decimal(str(attributes["cbm_per_carton"])) == Decimal("0.03")
    assert attributes["safety_stock_qty"] == 60
    assert variant["weight_grams"] == 20
    assert variant["lead_time_days"] == 12
    assert Decimal(variant["retail_price_usd"]) == Decimal("18.50")
    assert [(t["min_qty"], Decimal(t["unit_price_usd"])) for t in variant["b2b_moq_tiers"]] == [
        (100, Decimal("16.80")),
        (500, Decimal("15.40")),
    ]

    minimal = await async_client.get(
        "/api/v1/catalog/variants?sku=SKU-STRIP-10M", headers=headers
    )
    minimal_attributes = minimal.json()["items"][0]["attributes"]
    assert minimal_attributes["safety_stock_qty"] == 40
    assert minimal.json()["items"][0]["b2b_moq_tiers"] == []

    products = await async_client.get(
        "/api/v1/catalog/products?search=QZ-STRIP-5M", headers=headers
    )
    assert products.status_code == 200, products.text
    assert products.json()["total"] == 1
    product = products.json()["items"][0]
    assert product["category"] == "led_lighting"
    assert product["hs_code"] == "9405429000"
    assert product["i18n_specs"]["en"]["title"] == "5M IP65 RGBW LED Strip"

    # Re-importing the same sheet is idempotent for products and updates in place.
    repriced = sheet.replace("18.50,9.20", "19.99,9.20")
    again = await async_client.post(
        "/api/v1/catalog/variants/import-csv", headers=headers, json={"csv_text": repriced}
    )
    assert again.status_code == 200, again.text
    assert again.json() == {
        "created_products": 0,
        "created_variants": 0,
        "updated_variants": 3,
        "errors": [],
    }
    refreshed = await async_client.get(
        "/api/v1/catalog/variants?sku=SKU-STRIP-5M", headers=headers
    )
    assert Decimal(refreshed.json()["items"][0]["retail_price_usd"]) == Decimal("19.99")

    async with tenant_session(runtime_session_factory, UUID(tenant_id)) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM products WHERE tenant_id = :tid"),
                {"tid": UUID(tenant_id)},
            )
            == 2
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM product_variants WHERE tenant_id = :tid"),
                {"tid": UUID(tenant_id)},
            )
            == 3
        )


async def test_catalog_csv_import_reports_row_errors_and_guards_roles(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}

    partial = _csv(
        "QZ-BAD-1,Bad price row,led_lighting,9405429000,SKU-BAD-PRICE,not-a-number,,,,,,,,,",
        "QZ-GOOD,Good row,led_lighting,9405429000,SKU-GOOD,11.00,,,,10,,,,,",
        ",Missing SPU,led_lighting,9405429000,SKU-NO-SPU,9.00,,,,,,,,,",
    )
    response = await async_client.post(
        "/api/v1/catalog/variants/import-csv", headers=headers, json={"csv_text": partial}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_products"] == 1
    assert body["created_variants"] == 1
    assert body["updated_variants"] == 0
    assert len(body["errors"]) == 2
    assert any("Row 2" in error and "retail_price_usd" in error for error in body["errors"])
    assert any("Row 4" in error and "spu_code" in error for error in body["errors"])

    missing_header = await async_client.post(
        "/api/v1/catalog/variants/import-csv",
        headers=headers,
        json={"csv_text": "spu_code,product_name\nSPU-1,Widget"},
    )
    assert missing_header.status_code == 422
    assert missing_header.json()["code"] == "ERR_INVALID_CATALOG_CSV"

    agent_headers = await _member_headers(
        runtime_session_factory, UUID(tenant_id), UserRole.AGENT
    )
    forbidden = await async_client.post(
        "/api/v1/catalog/variants/import-csv",
        headers=agent_headers,
        json={"csv_text": _csv("QZ-A,Agent row,led,9405429000,SKU-AGENT,5.00,,,,,,,,")},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ERR_INSUFFICIENT_ROLE"


async def test_catalog_product_and_variant_create_accept_packing_specs(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}
    product = await async_client.post(
        "/api/v1/catalog/products",
        headers=headers,
        json={
            "spu_code": "QZ-FLOOD-50W",
            "category": "led_lighting",
            "hs_code": "9405429900",
            "i18n_specs": {"en": {"title": "50W Flood Light"}},
        },
    )
    assert product.status_code == 201, product.text
    variant = await async_client.post(
        "/api/v1/catalog/variants",
        headers=headers,
        json={
            "product_id": product.json()["id"],
            "sku": "SKU-FLOOD-50W",
            "attributes": {
                "carton_qty": 12,
                "gross_weight_kg": "9.60",
                "net_weight_kg": "8.80",
                "cbm_per_carton": "0.0720",
                "safety_stock_qty": 24,
                "image_url": "https://cdn.example.com/flood-50w.png",
            },
            "cost_price_base": "42.5000",
            "retail_price_usd": "89.00",
            "weight_grams": 733,
        },
    )
    assert variant.status_code == 201, variant.text
    attributes = variant.json()["attributes"]
    assert attributes["carton_qty"] == 12
    assert attributes["image_url"] == "https://cdn.example.com/flood-50w.png"
    assert Decimal(str(attributes["cbm_per_carton"])) == Decimal("0.0720")
    assert attributes["safety_stock_qty"] == 24
    async with tenant_session(runtime_session_factory, UUID(tenant_id)) as session:
        stored = await session.scalar(
            text(
                "SELECT attributes->>'carton_qty' FROM product_variants "
                "WHERE tenant_id = :tid AND sku = 'SKU-FLOOD-50W'"
            ),
            {"tid": UUID(tenant_id)},
        )
    assert stored == "12"


async def test_export_commercial_invoice_and_packing_list_pdfs(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}
    warehouse_id, customer_id = uuid4(), uuid4()

    _, strip_variant = await _seed_variant(
        runtime_session_factory,
        tenant_id,
        spu_code="QZ-STRIP-5M",
        sku="SKU-STRIP-5M",
        title="5M IP65 RGBW LED Strip",
        hs_code="9405429000",
        retail_price_usd="4.50",
        cost_price_base="2.10",
        attributes='{"carton_qty": 25, "gross_weight_kg": "0.62", '
        '"net_weight_kg": "0.50", "cbm_per_carton": "0.0300"}',
        on_hand=600,
        warehouse_id=warehouse_id,
    )
    _, panel_variant = await _seed_variant(
        runtime_session_factory,
        tenant_id,
        spu_code="QZ-PANEL-60X60",
        sku="SKU-PANEL-60X60",
        title="60x60 Backlit LED Panel",
        hs_code="9405409000",
        retail_price_usd="9.75",
        cost_price_base="5.40",
        attributes='{"carton_qty": 20, "gross_weight_kg": "1.10", '
        '"net_weight_kg": "0.95", "cbm_per_carton": "0.0200"}',
        on_hand=400,
        warehouse_id=warehouse_id,
    )
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name, primary_email, "
                "primary_phone_e164, country_code, customer_type) "
                "VALUES (:cid, :tid, 'Nordic Lighting AB', 'buyer@nordic-lighting.se', "
                "'+46701234567', 'SE', 'b2b_company_rep')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "UPDATE tenants SET ai_policy_settings = jsonb_set("
                " COALESCE(ai_policy_settings, '{}'::jsonb), '{pi_template}', CAST(:tpl AS jsonb))"
                " WHERE id = :tid"
            ),
            {
                "tid": tenant_id,
                "tpl": (
                    '{"company_legal_name": "Lumen Export Co Ltd",'
                    ' "company_address": "No. 88 Export Road, Xiamen, Fujian, China",'
                    ' "contact_phone": "+86 592 8888 6666",'
                    ' "contact_email": "sales@lumen-export.com",'
                    ' "tax_or_reg_no": "91350200MA2X8N7B2C",'
                    ' "bank_beneficiary_name": "Lumen Export Co Ltd",'
                    ' "bank_name": "Bank of China Xiamen Branch",'
                    ' "bank_account_no": "4067 8801 2233 4455",'
                    ' "bank_swift_code": "BKCHCNBJ74A",'
                    ' "bank_address": "Hubin North Road, Xiamen, China",'
                    ' "footer_signature_title": "Authorized Signature & Company Stamp"}'
                ),
            },
        )

    order = await async_client.post(
        "/api/v1/orders",
        headers=headers,
        json={
            "order_mode": "b2b_wholesale",
            "customer_id": str(customer_id),
            "warehouse_id": str(warehouse_id),
            "currency": "USD",
            "items": [
                {"variant_id": str(strip_variant), "quantity": 250, "unit_price": "4.50"},
                {"variant_id": str(panel_variant), "quantity": 120, "unit_price": "9.75"},
            ],
        },
    )
    assert order.status_code == 201, order.text
    order_no = order.json()["order_no"]
    order_id = order.json()["id"]

    invoice = await async_client.get(
        f"/api/v1/orders/{order_id}/documents/commercial-invoice.pdf",
        headers=headers,
    )
    assert invoice.status_code == 200, invoice.text
    assert invoice.headers["content-type"] == "application/pdf"
    assert f'filename="CI-{order_no}.pdf"' in invoice.headers["content-disposition"]
    assert invoice.content.startswith(b"%PDF-")
    invoice_text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(invoice.content)).pages
    )
    assert "COMMERCIAL INVOICE" in invoice_text
    assert f"CI-{order_no}" in invoice_text
    assert "9405429000" in invoice_text
    assert "9405409000" in invoice_text
    assert "BKCHCNBJ74A" in invoice_text
    assert "SWIFT" in invoice_text
    assert "Grand Total" in invoice_text
    assert "2295.00" in invoice_text
    assert "Authorized Signature & Company Stamp" in invoice_text
    assert "Nordic Lighting AB" in invoice_text

    packing = await async_client.get(
        f"/api/v1/orders/{order_id}/documents/packing-list.pdf",
        headers=headers,
    )
    assert packing.status_code == 200, packing.text
    assert packing.headers["content-type"] == "application/pdf"
    assert f'filename="PL-{order_no}.pdf"' in packing.headers["content-disposition"]
    assert packing.content.startswith(b"%PDF-")
    packing_text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(packing.content)).pages
    )
    assert "PACKING LIST" in packing_text
    assert f"PL-{order_no}" in packing_text
    assert "Units/Carton" in packing_text
    assert "Cartons" in packing_text
    assert "0.4200" in packing_text  # grand-total CBM: 10 * 0.03 + 6 * 0.02
    assert "6.20" in packing_text  # first line gross weight
    assert "Total Gross Weight" in packing_text
    assert "Shipping Marks" in packing_text

    chinese = await async_client.get(
        f"/api/v1/orders/{order_id}/documents/commercial-invoice.pdf?language=zh",
        headers=headers,
    )
    assert chinese.status_code == 200, chinese.text
    assert chinese.content.startswith(b"%PDF-")

    for role in (UserRole.AGENT, UserRole.FINANCE):
        role_headers = await _member_headers(runtime_session_factory, tenant_id, role)
        allowed = await async_client.get(
            f"/api/v1/orders/{order_id}/documents/packing-list.pdf?language=en",
            headers=role_headers,
        )
        assert allowed.status_code == 200, allowed.text

    other_token, _, _ = await _register_owner(async_client)
    foreign = await async_client.get(
        f"/api/v1/orders/{order_id}/documents/commercial-invoice.pdf",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert foreign.status_code == 404


async def test_wms_warehouse_creation_is_tenant_scoped(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}

    created = await async_client.post(
        "/api/v1/wms/warehouses",
        headers=headers,
        json={"code": "XM-A1", "name": "Xiamen Export Hub", "country_code": "CN", "city": "Xiamen"},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["code"] == "XM-A1"
    assert payload["name"] == "Xiamen Export Hub"
    assert payload["country_code"] == "CN"
    assert payload["city"] == "Xiamen"
    assert UUID(payload["id"])

    duplicate = await async_client.post(
        "/api/v1/wms/warehouses",
        headers=headers,
        json={"code": "XM-A1", "name": "Second hub"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ERR_STATE_CONFLICT"

    same_code_other_tenant, _, _ = await _register_owner(async_client)
    accepted = await async_client.post(
        "/api/v1/wms/warehouses",
        headers={"Authorization": f"Bearer {same_code_other_tenant}"},
        json={"code": "XM-A1", "name": "Another tenant hub"},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["id"] != payload["id"]

    agent_headers = await _member_headers(
        runtime_session_factory, tenant_id, UserRole.AGENT
    )
    forbidden = await async_client.post(
        "/api/v1/wms/warehouses",
        headers=agent_headers,
        json={"code": "XM-A2", "name": "Agent hub"},
    )
    assert forbidden.status_code == 403


async def test_wms_low_stock_alerts_and_one_click_purchase_order(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}

    warehouse = await async_client.post(
        "/api/v1/wms/warehouses",
        headers=headers,
        json={"code": "FJ-01", "name": "Fujian Main Warehouse", "city": "Putian"},
    )
    assert warehouse.status_code == 201, warehouse.text
    warehouse_id = UUID(warehouse.json()["id"])

    _, short_variant = await _seed_variant(
        runtime_session_factory,
        tenant_id,
        spu_code="QZ-GU10-9W",
        sku="SKU-GU10-9W",
        title="9W GU10 Spotlight",
        attributes='{"carton_qty": 40, "safety_stock_qty": 120, "net_weight_kg": "2.40", '
        '"gross_weight_kg": "2.80", "cbm_per_carton": "0.0180"}',
        on_hand=100,
        reserved=30,
        warehouse_id=warehouse_id,
    )
    _, healthy_variant = await _seed_variant(
        runtime_session_factory,
        tenant_id,
        spu_code="QZ-PANEL-30X30",
        sku="SKU-PANEL-30X30",
        title="30x30 LED Panel",
        attributes='{"carton_qty": 30, "safety_stock_qty": 50}',
        on_hand=400,
        reserved=20,
        warehouse_id=warehouse_id,
    )
    _, default_variant = await _seed_variant(
        runtime_session_factory,
        tenant_id,
        spu_code="QZ-E27-12W",
        sku="SKU-E27-12W",
        title="12W E27 Bulb",
        attributes="{}",
        on_hand=20,
        reserved=5,
        warehouse_id=warehouse_id,
    )

    low_stock = await async_client.get("/api/v1/wms/low-stock", headers=headers)
    assert low_stock.status_code == 200, low_stock.text
    body = low_stock.json()
    by_variant = {item["variant_id"]: item for item in body["items"]}
    assert str(healthy_variant) not in by_variant
    short = by_variant[str(short_variant)]
    assert short["sku"] == "SKU-GU10-9W"
    assert short["product_name"] == "9W GU10 Spotlight"
    assert short["warehouse_id"] == str(warehouse_id)
    assert short["warehouse_name"] == "Fujian Main Warehouse"
    assert short["on_hand_qty"] == 100
    assert short["reserved_qty"] == 30
    assert short["available_qty"] == 70
    assert short["safety_stock_qty"] == 120
    assert short["suggested_reorder_qty"] == 170
    default_row = by_variant[str(default_variant)]
    assert default_row["safety_stock_qty"] == 50
    assert default_row["available_qty"] == 15
    assert default_row["suggested_reorder_qty"] == 85
    assert body["total"] == 2

    supplier = await async_client.post(
        "/api/v1/supply-chain/suppliers",
        headers=headers,
        json={"code": "SUP-GU10", "name": "Guangdong Lamp Works", "lead_time_days": 25},
    )
    assert supplier.status_code == 201, supplier.text
    supplier_id = supplier.json()["id"]

    po = await async_client.post(
        "/api/v1/wms/low-stock/create-po",
        headers=headers,
        json={
            "supplier_id": supplier_id,
            "variant_id": str(short_variant),
            "quantity": 500,
            "unit_cost": "2.5000",
            "currency": "USD",
            "notes": "Auto restock from low-stock alert",
        },
    )
    assert po.status_code == 201, po.text
    created = po.json()
    assert created["status"] == "draft"
    assert created["supplier_id"] == supplier_id
    assert created["warehouse_id"] == str(warehouse_id)
    assert Decimal(created["total_amount"]) == Decimal("1250.00")
    assert created["notes"] == "Auto restock from low-stock alert"
    assert len(created["items"]) == 1
    assert created["items"][0]["sku"] == "SKU-GU10-9W"
    assert created["items"][0]["qty_ordered"] == 500
    assert Decimal(created["items"][0]["unit_cost"]) == Decimal("2.5000")

    listed = await async_client.get("/api/v1/supply-chain/purchase-orders", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["po_no"] for row in listed.json()["items"]] == [created["po_no"]]

    unknown_supplier = await async_client.post(
        "/api/v1/wms/low-stock/create-po",
        headers=headers,
        json={
            "supplier_id": str(uuid4()),
            "variant_id": str(short_variant),
            "quantity": 10,
            "unit_cost": "2.5000",
        },
    )
    assert unknown_supplier.status_code == 404

    agent_headers = await _member_headers(
        runtime_session_factory, tenant_id, UserRole.AGENT
    )
    allowed_read = await async_client.get("/api/v1/wms/low-stock", headers=agent_headers)
    assert allowed_read.status_code == 200, allowed_read.text
    forbidden_po = await async_client.post(
        "/api/v1/wms/low-stock/create-po",
        headers=agent_headers,
        json={
            "supplier_id": supplier_id,
            "variant_id": str(short_variant),
            "quantity": 10,
            "unit_cost": "2.5000",
        },
    )
    assert forbidden_po.status_code == 403


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_export_documents_require_a_real_order(
    async_client: AsyncClient,
    language: str,
) -> None:
    token, _, _ = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}
    missing = await async_client.get(
        f"/api/v1/orders/{uuidlib.uuid4()}/documents/commercial-invoice.pdf?language={language}",
        headers=headers,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "ERR_ORDER_NOT_FOUND"
