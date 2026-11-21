"""Wave 2 regressions: ecommerce FX honesty, line-total math, order amount guards.

Covers GPT #07 / Muse P0-07 (no blanket 7.2 FX default), Muse P1-12 (line totals and
refunded revenue), and Muse P1-01 (non-negative amounts + maskable cost snapshot).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from erp_core.channels.ecommerce.base import (
    USD_BASE_REFERENCE_RATES,
    NormalizedOrder,
    resolve_order_fx_rate,
)
from erp_core.channels.ecommerce.shoplazza import ShoplazzaAdapter
from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
from erp_core.finance.fx_service import _FALLBACK_USD_RATES
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderCreateRequest, OrderItemRead
from erp_core.shared.enums import PaymentStatus
from erp_core.shared.errors import AppError

SIX_DP = Decimal("0.000001")


def test_identity_rate_for_same_order_and_base_currency() -> None:
    for code in ("CNY", "USD", "EUR"):
        rate, source = resolve_order_fx_rate(
            order_currency=code,
            base_currency=code.lower(),
            explicit_rate=None,
        )
        assert (rate, source) == (Decimal("1.000000"), "identity")
        assert rate == Decimal("1").quantize(SIX_DP)


def test_reference_table_matches_finance_fallback_quotes() -> None:
    """Drift guard: the ecommerce reference rates must mirror finance's fallback quotes."""
    assert {
        code: rate for code, rate in USD_BASE_REFERENCE_RATES.items() if code != "USD"
    } == _FALLBACK_USD_RATES


def test_eur_to_cny_cross_rate_is_not_the_blanket_7_2() -> None:
    rate, source = resolve_order_fx_rate(
        order_currency="EUR",
        base_currency="CNY",
        explicit_rate=None,
    )
    # 7.2400 CNY/USD / 0.9200 EUR/USD == 7.869565 CNY per EUR (ROUND_HALF_UP, 6 dp).
    assert rate == Decimal("7.869565")
    assert rate != Decimal("7.200000")
    assert source == "fallback_fx"


def test_usd_to_cny_uses_the_quoted_reference_rate() -> None:
    rate, source = resolve_order_fx_rate(
        order_currency="USD", base_currency="CNY", explicit_rate=None
    )
    assert rate == Decimal("7.240000")
    assert source == "fallback_fx"


def test_live_usd_base_quotes_are_preferred_over_the_reference_table() -> None:
    rate, source = resolve_order_fx_rate(
        order_currency="EUR",
        base_currency="CNY",
        explicit_rate=None,
        live_rates_usd_base={"USD": Decimal("1"), "CNY": Decimal("7.0"), "EUR": Decimal("0.9")},
    )
    # 7.0 / 0.9 == 7.777778 CNY per EUR.
    assert rate == Decimal("7.777778")
    assert source == "live_fx"


def test_explicit_rate_wins_and_is_quantized_to_six_decimals() -> None:
    rate, source = resolve_order_fx_rate(
        order_currency="EUR",
        base_currency="CNY",
        explicit_rate=Decimal("7.1234567"),
    )
    assert (rate, source) == (Decimal("7.123457"), "explicit")


def test_non_positive_explicit_rate_is_rejected_instead_of_silently_cross_converted() -> None:
    with pytest.raises(AppError):
        resolve_order_fx_rate(
            order_currency="EUR", base_currency="CNY", explicit_rate=Decimal("0")
        )


def test_currency_without_a_quote_is_rejected() -> None:
    with pytest.raises(AppError):
        resolve_order_fx_rate(order_currency="EUR", base_currency="XXX", explicit_rate=None)
    with pytest.raises(AppError):
        resolve_order_fx_rate(order_currency="XYZ", base_currency="CNY", explicit_rate=None)


def test_normalized_order_no_longer_defaults_to_a_7_2_rate() -> None:
    dto = NormalizedOrder(
        external_order_id="ext-1",
        currency="EUR",
        subtotal_amount=Decimal("10.00"),
        total_amount=Decimal("10.00"),
        customer={"email": "buyer@example.com"},
        line_items=[
            {"external_sku": "SKU-1", "quantity": 1, "line_total": Decimal("10.00")}
        ],
        updated_at=datetime.now(UTC),
    )
    assert dto.fx_rate_to_base is None


def _tiktok_order(*, quantity: int, sale_price: str) -> dict[str, Any]:
    return {
        "id": "TT-ORD-90001",
        "status": "AWAITING_SHIPMENT",
        "update_time": 1763510500,
        "buyer_email": "tt.shopper@example.com",
        "user_id": "tt_buyer_991",
        "recipient_address": {
            "name": "Mia Torres",
            "phone_number": "+14155550199",
            "region_code": "US",
        },
        "payment": {
            "currency": "USD",
            "sub_total": "225.00",
            "shipping_fee": "5.00",
            "tax": "0.00",
            "total_amount": "230.00",
            "platform_discount": "0.00",
        },
        "line_items": [
            {
                "id": "li_1",
                "seller_sku": "TT-LED-5M-1PK",
                "product_name": "LED Strip",
                "quantity": quantity,
                "sale_price": sale_price,
            }
        ],
        "packages": [{"id": "pkg_551"}],
    }


def test_tiktok_line_total_multiplies_unit_sale_price_by_quantity() -> None:
    adapter = TikTokShopCoreAdapter(
        app_key="app_key",
        app_secret="app_secret",
        access_token="token",
        shop_cipher="cipher",
    )
    normalized = adapter._normalize_order(_tiktok_order(quantity=5, sale_price="45.00"))
    assert normalized.line_items[0].quantity == 5
    assert normalized.line_items[0].line_total == Decimal("225.00")
    # 5 x 45.00 must not collapse back to a single unit price.
    assert normalized.line_items[0].line_total != Decimal("45.00")
    assert normalized.fx_rate_to_base is None


def test_tiktok_explicit_line_total_field_is_honoured() -> None:
    adapter = TikTokShopCoreAdapter(
        app_key="app_key",
        app_secret="app_secret",
        access_token="token",
        shop_cipher="cipher",
    )
    order = _tiktok_order(quantity=5, sale_price="45.00")
    order["line_items"][0]["line_total"] = "200.00"
    normalized = adapter._normalize_order(order)
    assert normalized.line_items[0].line_total == Decimal("200.00")


def _shoplazza_order(line_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "spz_ord_401",
        "number": "#SL-401",
        "currency": "USD",
        "subtotal_price": "100.00",
        "total_shipping_price": "0.00",
        "total_tax": "0.00",
        "total_price": "100.00",
        "financial_status": "paid",
        "updated_at": "2026-11-19T01:05:00Z",
        "customer": {"email": "ops@pacificled.au", "first_name": "Liam"},
        "line_items": [line_item],
    }


def test_shoplazza_line_total_multiplies_unit_price_by_quantity() -> None:
    adapter = ShoplazzaAdapter(store_domain="qz.myshoplaza.com", access_token="spz_tok")
    normalized = adapter._normalize_order(
        _shoplazza_order(
            {"sku": "LED-STRIP-5M", "title": "5M Strip", "quantity": 5, "price": "20.00"}
        )
    )
    assert normalized.line_items[0].quantity == 5
    assert normalized.line_items[0].line_total == Decimal("100.00")
    assert normalized.line_items[0].line_total != Decimal("20.00")
    assert normalized.fx_rate_to_base is None


def test_shoplazza_explicit_total_field_is_honoured() -> None:
    adapter = ShoplazzaAdapter(store_domain="qz.myshoplaza.com", access_token="spz_tok")
    normalized = adapter._normalize_order(
        _shoplazza_order(
            {
                "sku": "LED-STRIP-5M",
                "title": "5M Strip",
                "quantity": 5,
                "price": "20.00",
                "total": "88.00",
            }
        )
    )
    assert normalized.line_items[0].line_total == Decimal("88.00")


def _order_request_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "customer_id": uuid4(),
        "warehouse_id": uuid4(),
        "currency": "USD",
        "items": [{"variant_id": uuid4(), "quantity": 1, "unit_price": "10.00"}],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "field",
    ["total_amount", "subtotal_amount", "shipping_charged", "discount_amount", "tax_collected"],
)
def test_order_create_request_rejects_negative_amounts(field: str) -> None:
    with pytest.raises(ValidationError):
        OrderCreateRequest.model_validate(_order_request_payload(**{field: "-1.00"}))


def test_order_create_request_rejects_negative_computed_total() -> None:
    with pytest.raises(ValidationError):
        OrderCreateRequest.model_validate(
            _order_request_payload(subtotal_amount="10.00", discount_amount="50.00")
        )


def test_order_create_request_defaults_fx_rate_to_none_and_accepts_zero_total() -> None:
    request = OrderCreateRequest.model_validate(
        _order_request_payload(subtotal_amount="0.00", total_amount="0.00")
    )
    assert request.fx_rate_to_base is None
    assert request.total_amount == Decimal("0.00")


def test_order_item_read_allows_a_masked_cost_snapshot() -> None:
    item = OrderItemRead(
        id=uuid4(),
        order_id=uuid4(),
        variant_id=uuid4(),
        sku_snapshot="LED-STRIP-5M",
        quantity=2,
        unit_price=Decimal("10.0000"),
        line_total=Decimal("20.00"),
    )
    assert item.unit_cost_base_snapshot is None
    masked = item.model_copy(update={"unit_cost_base_snapshot": None})
    assert masked.unit_cost_base_snapshot is None


def test_refunded_status_writes_back_the_refunded_amount_for_cvp() -> None:
    metadata = OrderService._with_refunded_amount(
        {"po_number": "PO-1"}, PaymentStatus.REFUNDED, Decimal("50.00")
    )
    assert metadata["refunded_amount"] == "50.00"
    assert metadata["po_number"] == "PO-1"


def test_refunded_amount_from_the_channel_is_never_overwritten() -> None:
    metadata = OrderService._with_refunded_amount(
        {"refunded_amount": "12.50"}, PaymentStatus.REFUNDED, Decimal("50.00")
    )
    assert metadata["refunded_amount"] == "12.50"


def test_non_refunded_statuses_do_not_write_back_a_refunded_amount() -> None:
    metadata = OrderService._with_refunded_amount({}, PaymentStatus.PAID, Decimal("50.00"))
    assert "refunded_amount" not in metadata
