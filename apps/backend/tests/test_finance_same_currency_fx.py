"""Unit tests for same-currency settlement FX rate enforcement."""

from __future__ import annotations

from decimal import Decimal

import pytest

from erp_core.finance.payment_service import validate_same_currency_fx_rate
from erp_core.shared.errors import AppError


def test_same_currency_settlement_rejects_non_unit_fx_rate() -> None:
    with pytest.raises(AppError) as exc_info:
        validate_same_currency_fx_rate(
            payment_currency="USD",
            base_currency="USD",
            actual_fx_rate=Decimal("7.12"),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "ERR_INVALID_SAME_CURRENCY_FX"


def test_same_currency_settlement_accepts_unit_fx_rate() -> None:
    rate = validate_same_currency_fx_rate(
        payment_currency="usd",
        base_currency="USD",
        actual_fx_rate=Decimal("1"),
    )
    assert rate == Decimal("1.000000")


def test_cross_currency_settlement_accepts_live_fx_rate() -> None:
    rate = validate_same_currency_fx_rate(
        payment_currency="USD",
        base_currency="CNY",
        actual_fx_rate=Decimal("7.1234567"),
    )
    assert rate == Decimal("7.123457")
