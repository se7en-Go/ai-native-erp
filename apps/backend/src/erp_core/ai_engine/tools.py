from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import Incoterm

_MONEY_Q = Decimal("0.01")
_UNIT_PRICE_Q = Decimal("0.0001")

_ANALYTICS_COLUMNS = {
    "vw_tenant_order_profit_summary": frozenset(
        {
            "tenant_id",
            "order_mode",
            "currency",
            "fulfillment_status",
            "payment_status",
            "order_day",
            "order_count",
            "total_order_amount",
            "gross_revenue_base",
            "cogs_base",
            "net_contribution_margin_base",
        }
    ),
    "vw_tenant_inventory_risk_summary": frozenset(
        {
            "tenant_id",
            "warehouse_id",
            "variant_id",
            "sku",
            "spu_code",
            "category",
            "on_hand_qty",
            "reserved_qty",
            "available_qty",
            "lead_time_days",
        }
    ),
}
_NAME = r"[a-z][a-z0-9_]{0,62}"
_SELECT = re.compile(
    rf"select\s+(.+?)\s+from\s+({_NAME})(?:\s+group\s+by\s+(.+))?", re.ASCII | re.I
)
_PROJECTION = re.compile(
    rf"(?:(sum|count|min|max|avg)\s*\(\s*({_NAME}|\*)\s*\)|({_NAME}))"
    rf"(?:\s+as\s+({_NAME}))?",
    re.ASCII | re.I,
)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValueError("An exact decimal is required.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal.") from exc
    if not number.is_finite() or abs(number) > Decimal("1e14"):
        raise ValueError("Decimal out of range.")
    return number


async def _pricing_policy(session: AsyncSession, tenant_id: UUID) -> dict[str, Any]:
    raw = await session.scalar(
        text("""
        SELECT CAST(ai_policy_settings -> 'quote_pricing' AS text) FROM tenants WHERE id = :tid
    """),
        {"tid": tenant_id},
    )
    data = json.loads(raw, parse_float=Decimal) if raw is not None else {}
    if not isinstance(data, dict):
        raise ValueError("Tenant quote_pricing must be a JSON object.")
    return {**data}


def _configured_number(policy: dict[str, Any], field: str, key: str) -> Decimal:
    values = policy.get(field)
    if not isinstance(values, dict) or key not in values:
        raise ValueError(f"No authoritative {field} value configured for {key}.")
    return _decimal(values[key])


def _usd_rate(policy: dict[str, Any], currency: str) -> Decimal:
    if currency == "USD":
        return Decimal(1)
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Invalid currency code.")
    rate = _configured_number(policy, "usd_to_currency", currency)
    if rate <= 0:
        raise ValueError("Exchange rate must be positive.")
    return rate


def _surcharge(policy: dict[str, Any], incoterm: Incoterm) -> Decimal:
    # Tenant contract stores the surcharge as a fraction (0.045 is 4.5%).
    surcharge = _configured_number(policy, "incoterm_surcharge_pct", incoterm.value)
    if not Decimal(0) <= surcharge <= Decimal(10):
        raise ValueError("Invalid Incoterm surcharge fraction.")
    return surcharge


class ToolResult(BaseModel):
    tool_call_id: str = Field(default_factory=lambda: f"tc_{uuid4().hex}")
    tool_name: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    numeric_tokens: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


def _convert_decimal_currency(
    policy: dict[str, Any],
    amount: Decimal,
    from_ccy: str,
    to_ccy: str,
) -> Decimal:
    if from_ccy == to_ccy:
        return amount
    return amount / _usd_rate(policy, from_ccy) * _usd_rate(policy, to_ccy)


def _extract_tool_tokens(value: Any, out: list[str]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, Decimal):
        out.append(str(value))
        return
    if isinstance(value, int):
        out.append(str(value))
        return
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?|[A-Z0-9][A-Z0-9-]{5,}", stripped):
            out.append(stripped)
        return
    if isinstance(value, list):
        for item in value:
            _extract_tool_tokens(item, out)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {
                "tool_call_id",
                "source_tool_call_id",
                "id",
                "variant_id",
                "order_id",
                "rp_id",
            }:
                continue
            _extract_tool_tokens(v, out)


def _select_moq_tier_price(
    tiers_json: list[dict[str, Any]] | None,
    retail_price_usd: Decimal,
    qty: int,
) -> tuple[Decimal, int | None]:
    if not tiers_json:
        return retail_price_usd, None
    sorted_tiers = sorted(
        tiers_json,
        key=lambda t: int(t.get("min_qty", 0)),
        reverse=True,
    )
    for tier in sorted_tiers:
        min_qty = int(tier.get("min_qty", 0))
        if qty >= min_qty and "unit_price_usd" in tier:
            return _decimal(tier["unit_price_usd"]), min_qty
    return retail_price_usd, None


def validate_readonly_analytics_sql(raw_sql: str) -> tuple[bool, str, str | None]:
    """Allow a minimal SELECT grammar; compile trusted identifiers and inject tenant binding."""
    cleaned = " ".join(raw_sql.strip().removesuffix(";").split()).lower()
    if not cleaned:
        return False, "ERR_EMPTY_ANALYTICS_SQL", None
    match = _SELECT.fullmatch(cleaned) if len(cleaned) <= 2048 else None
    if match is None:
        return False, "ERR_UNSAFE_ANALYTICS_SQL", None
    selection, view, group = match.groups()
    columns = _ANALYTICS_COLUMNS.get(view)
    if columns is None:
        return False, "ERR_DISALLOWED_ANALYTICS_RELATION", None
    groups = [name.strip() for name in group.split(",")] if group else []
    if not set(groups) <= columns:
        return False, "ERR_UNSAFE_ANALYTICS_SQL", None
    projections = []
    parts = [expr.strip() for expr in selection.split(",")]
    if parts == ["*"] and not groups:
        projections = [f'"{name}"' for name in sorted(columns)]
    else:
        for expr in parts:
            column = _PROJECTION.fullmatch(expr)
            if column is None:
                return False, "ERR_UNSAFE_ANALYTICS_SQL", None
            aggregate, argument, plain, alias = column.groups()
            if plain in columns:
                sql_expr = f'"{plain}"'
            elif aggregate and argument in columns:
                sql_expr = f'{aggregate}("{argument}")'
            elif aggregate == "count" and argument == "*":
                sql_expr = "count(*)"
            else:
                return False, "ERR_UNSAFE_ANALYTICS_SQL", None
            projections.append(sql_expr + (f' AS "{alias}"' if alias else ""))
    sql = f'SELECT {", ".join(projections)} FROM public."{view}" WHERE tenant_id = :tid'
    if groups:
        sql += " GROUP BY " + ", ".join(f'"{name}"' for name in groups)
    return (
        True,
        "",
        sql + " ORDER BY " + ", ".join(str(i + 1) for i in range(len(projections))) + " LIMIT 100",
    )


class ErpToolRegistry:
    """Strictly RLS-scoped deterministic ERP SQL tools with source_tool_call_id provenance."""

    @staticmethod
    def _failure(tool: str, code: str, message: str) -> ToolResult:
        return ToolResult(tool_name=tool, ok=False, error_code=code, error_message=message)

    async def get_live_sku_pricing_and_stock(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        sku: str,
        qty: int = 1,
        incoterm: Incoterm = Incoterm.FOB,
    ) -> ToolResult:
        row = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        pv.id AS variant_id,
                        pv.sku,
                        p.spu_code,
                        p.category,
                        p.hs_code,
                        p.certifications,
                        pv.retail_price_usd,
                        CAST(pv.b2b_moq_tiers AS text) AS tiers_json,
                        pv.lead_time_days,
                        COALESCE(SUM(ib.on_hand_qty), 0) AS on_hand_qty,
                        COALESCE(SUM(ib.reserved_qty), 0) AS reserved_qty,
                        COALESCE(SUM(ib.on_hand_qty - ib.reserved_qty), 0) AS available_qty
                    FROM product_variants pv
                    JOIN products p
                        ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id
                    LEFT JOIN inventory_balances ib
                        ON ib.variant_id = pv.id AND ib.tenant_id = pv.tenant_id
                    WHERE pv.tenant_id = :tid
                      AND pv.sku = :sku
                    GROUP BY pv.id, p.id
                    """
                    ),
                    {"tid": tenant_id, "sku": sku.strip()},
                )
            )
            .mappings()
            .first()
        )

        if row is None:
            return ToolResult(
                tool_name="get_live_sku_pricing_and_stock",
                ok=False,
                error_code="ERR_SKU_NOT_FOUND",
                error_message=f"SKU '{sku}' not found.",
            )

        if type(qty) is not int or not 0 < qty <= 2_147_483_647:
            return self._failure(
                "get_live_sku_pricing_and_stock",
                "ERR_INVALID_QUANTITY",
                "Quantity must be a positive integer.",
            )
        try:
            surcharge = _surcharge(await _pricing_policy(session, tenant_id), incoterm)
        except ValueError as exc:
            return self._failure("get_live_sku_pricing_and_stock", "ERR_PRICING_CONFIG", str(exc))
        tiers = json.loads(row["tiers_json"], parse_float=Decimal)
        retail_usd = Decimal(str(row["retail_price_usd"])).quantize(
            _MONEY_Q, rounding=ROUND_HALF_UP
        )
        base_unit_usd, tier_applied = _select_moq_tier_price(tiers, retail_usd, qty)
        adjusted_unit_usd = (base_unit_usd * (Decimal("1.0000") + surcharge)).quantize(
            _MONEY_Q, rounding=ROUND_HALF_UP
        )
        line_total_usd = (adjusted_unit_usd * Decimal(qty)).quantize(
            _MONEY_Q, rounding=ROUND_HALF_UP
        )

        result = ToolResult(
            tool_name="get_live_sku_pricing_and_stock",
            ok=True,
            data={
                "sku": row["sku"],
                "spu_code": row["spu_code"],
                "category": row["category"],
                "hs_code": row["hs_code"],
                "certifications": list(row["certifications"] or []),
                "qty_requested": qty,
                "incoterm": incoterm.value,
                "retail_price_usd": str(retail_usd),
                "applied_moq_tier": tier_applied,
                "applied_unit_price_usd": str(adjusted_unit_usd),
                "line_total_usd": str(line_total_usd),
                "b2b_moq_tiers": [
                    {"min_qty": tier["min_qty"], "unit_price_usd": str(tier["unit_price_usd"])}
                    for tier in tiers
                ],
                "on_hand_qty": int(row["on_hand_qty"]),
                "reserved_qty": int(row["reserved_qty"]),
                "available_qty": int(row["available_qty"]),
                "lead_time_days": int(row["lead_time_days"]),
            },
        )
        result.data["source_tool_call_id"] = result.tool_call_id
        _extract_tool_tokens(result.data, result.numeric_tokens)
        return result

    async def get_live_order_tracking(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_no: str,
    ) -> ToolResult:
        order_row = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        id, order_no, order_mode, currency, total_amount,
                        fulfillment_status, payment_status, created_at
                    FROM orders
                    WHERE tenant_id = :tid AND order_no = :order_no
                    LIMIT 1
                    """
                    ),
                    {"tid": tenant_id, "order_no": order_no.strip()},
                )
            )
            .mappings()
            .first()
        )

        if order_row is None:
            return ToolResult(
                tool_name="get_live_order_tracking",
                ok=False,
                error_code="ERR_ORDER_NOT_FOUND",
                error_message=f"Order '{order_no}' not found.",
            )

        shipment_rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        tracking_provider, tracking_number, carrier_code, shipment_mode,
                        standardized_status, sub_status, last_event_at,
                        estimated_delivery_at, tracking_events
                    FROM shipments
                    WHERE tenant_id = :tid AND order_id = :oid AND tracking_provider = '17track'
                    ORDER BY created_at DESC, id
                    """
                    ),
                    {"tid": tenant_id, "oid": order_row["id"]},
                )
            )
            .mappings()
            .all()
        )

        shipments_payload: list[dict[str, Any]] = []
        for s in shipment_rows:
            shipments_payload.append(
                {
                    "tracking_provider": s["tracking_provider"],
                    "tracking_number": s["tracking_number"],
                    "carrier_code": s["carrier_code"],
                    "shipment_mode": s["shipment_mode"],
                    "standardized_status": s["standardized_status"],
                    "sub_status": s["sub_status"],
                    "last_event_at": s["last_event_at"].isoformat() if s["last_event_at"] else None,
                    "estimated_delivery_at": (
                        s["estimated_delivery_at"].isoformat()
                        if s["estimated_delivery_at"]
                        else None
                    ),
                    "latest_event": (s["tracking_events"][-1] if s["tracking_events"] else None),
                }
            )

        result = ToolResult(
            tool_name="get_live_order_tracking",
            ok=True,
            data={
                "order_id": str(order_row["id"]),
                "order_no": order_row["order_no"],
                "order_mode": order_row["order_mode"],
                "currency": order_row["currency"],
                "total_amount": str(
                    Decimal(str(order_row["total_amount"])).quantize(
                        _MONEY_Q, rounding=ROUND_HALF_UP
                    )
                ),
                "fulfillment_status": order_row["fulfillment_status"],
                "payment_status": order_row["payment_status"],
                "shipments": shipments_payload,
            },
        )
        result.data["source_tool_call_id"] = result.tool_call_id
        _extract_tool_tokens(result.data, result.numeric_tokens)
        return result

    async def calc_b2b_quote_matrix(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        items: list[dict[str, Any]],
        currency: str,
        incoterm: Incoterm,
    ) -> ToolResult:
        if not items:
            return ToolResult(
                tool_name="calc_b2b_quote_matrix",
                ok=False,
                error_code="ERR_EMPTY_QUOTE_ITEMS",
                error_message="Quote calculation requires at least one item.",
            )

        target_ccy = currency.upper()
        try:
            policy = await _pricing_policy(session, tenant_id)
            quote_rate = _usd_rate(policy, target_ccy)
            surcharge = _surcharge(policy, incoterm)
            base_ccy = await session.scalar(
                text("SELECT base_currency FROM tenants WHERE id = :tid"), {"tid": tenant_id}
            )
            if base_ccy is None:
                raise ValueError("Tenant base currency is unavailable.")
            base_rate = _usd_rate(policy, base_ccy)
        except ValueError as exc:
            return self._failure("calc_b2b_quote_matrix", "ERR_PRICING_CONFIG", str(exc))

        line_items: list[dict[str, Any]] = []
        total_revenue_quote_ccy = Decimal("0.00")
        total_revenue_base = Decimal("0.00")
        total_cost_base = Decimal("0.00")
        below_floor = False
        max_lead_time_days = 0

        for item in items:
            sku = str(item.get("sku", "")).strip()
            qty = item.get("qty", 1)
            if type(qty) is not int or not 0 < qty <= 2_147_483_647:
                return ToolResult(
                    tool_name="calc_b2b_quote_matrix",
                    ok=False,
                    error_code="ERR_INVALID_QUANTITY",
                    error_message=f"Invalid quantity {qty} for SKU '{sku}'.",
                )

            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT
                            pv.id AS variant_id,
                            pv.sku,
                            pv.cost_price_base,
                            pv.base_currency,
                            pv.min_margin_floor_pct,
                            pv.retail_price_usd,
                            CAST(pv.b2b_moq_tiers AS text) AS tiers_json,
                            pv.lead_time_days,
                            p.spu_code
                        FROM product_variants pv
                        JOIN products p ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id
                        WHERE pv.tenant_id = :tid AND pv.sku = :sku
                        LIMIT 1
                        """
                        ),
                        {"tid": tenant_id, "sku": sku},
                    )
                )
                .mappings()
                .first()
            )

            if row is None:
                return ToolResult(
                    tool_name="calc_b2b_quote_matrix",
                    ok=False,
                    error_code="ERR_SKU_NOT_FOUND",
                    error_message=f"SKU '{sku}' not found.",
                )

            tiers = json.loads(row["tiers_json"], parse_float=Decimal)
            retail_usd = Decimal(str(row["retail_price_usd"])).quantize(
                _MONEY_Q, rounding=ROUND_HALF_UP
            )
            tier_unit_usd, tier_qty = _select_moq_tier_price(tiers, retail_usd, qty)
            standard_unit_quote = _convert_decimal_currency(
                policy,
                tier_unit_usd * (Decimal("1.0000") + surcharge),
                "USD",
                target_ccy,
            ).quantize(_UNIT_PRICE_Q, rounding=ROUND_HALF_UP)

            if item.get("requested_unit_price") is not None:
                try:
                    requested = _decimal(item["requested_unit_price"])
                    if requested <= 0:
                        raise ValueError("Requested price must be positive.")
                    unit_price_quote = requested.quantize(_UNIT_PRICE_Q, rounding=ROUND_HALF_UP)
                except ValueError as exc:
                    return self._failure("calc_b2b_quote_matrix", "ERR_INVALID_PRICE", str(exc))
            else:
                unit_price_quote = standard_unit_quote

            line_total = (unit_price_quote * Decimal(qty)).quantize(
                _MONEY_Q, rounding=ROUND_HALF_UP
            )
            cost_base = Decimal(str(row["cost_price_base"])).quantize(
                _UNIT_PRICE_Q, rounding=ROUND_HALF_UP
            )
            variant_ccy = str(row["base_currency"]).upper()
            try:
                unit_rev_in_base = _convert_decimal_currency(
                    policy,
                    unit_price_quote,
                    target_ccy,
                    variant_ccy,
                )
                variant_to_base = _convert_decimal_currency(
                    policy,
                    Decimal(1),
                    variant_ccy,
                    base_ccy,
                )
            except ValueError as exc:
                return self._failure("calc_b2b_quote_matrix", "ERR_PRICING_CONFIG", str(exc))
            line_rev_base = unit_rev_in_base * Decimal(qty)
            line_cost_base = cost_base * Decimal(qty)

            if line_rev_base <= Decimal("0"):
                line_margin_pct = Decimal("-100.00")
            else:
                line_margin_pct = (
                    ((line_rev_base - line_cost_base) / line_rev_base) * Decimal("100")
                ).quantize(_MONEY_Q, rounding=ROUND_HALF_UP)

            floor_pct = Decimal(str(row["min_margin_floor_pct"])).quantize(
                _MONEY_Q, rounding=ROUND_HALF_UP
            )
            line_below_floor = line_margin_pct < floor_pct
            if line_below_floor:
                below_floor = True

            total_revenue_quote_ccy += line_total
            total_revenue_base += line_rev_base * variant_to_base
            total_cost_base += line_cost_base * variant_to_base
            max_lead_time_days = max(max_lead_time_days, int(row["lead_time_days"]))

            line_items.append(
                {
                    "variant_id": str(row["variant_id"]),
                    "sku": row["sku"],
                    "spu_code": row["spu_code"],
                    "quantity": qty,
                    "moq_tier_applied": tier_qty,
                    "unit_price_quote_ccy": str(unit_price_quote),
                    "line_total_quote_ccy": str(line_total),
                    "line_margin_pct": str(line_margin_pct),
                    "below_margin_floor": line_below_floor,
                }
            )

        if total_revenue_base <= Decimal("0"):
            overall_margin_pct = Decimal("-100.00")
        else:
            overall_margin_pct = (
                ((total_revenue_base - total_cost_base) / total_revenue_base) * Decimal("100")
            ).quantize(_MONEY_Q, rounding=ROUND_HALF_UP)

        fx_snapshot = (base_rate / quote_rate).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

        result = ToolResult(
            tool_name="calc_b2b_quote_matrix",
            ok=True,
            data={
                "currency": target_ccy,
                "base_currency": base_ccy,
                "fx_rate_snapshot": str(fx_snapshot),
                "incoterm": incoterm.value,
                "items": line_items,
                "total_amount": str(
                    total_revenue_quote_ccy.quantize(_MONEY_Q, rounding=ROUND_HALF_UP)
                ),
                "estimated_margin_pct": str(overall_margin_pct),
                "below_margin_floor": below_floor,
                "lead_time_days": max_lead_time_days,
            },
        )
        result.data["source_tool_call_id"] = result.tool_call_id
        _extract_tool_tokens(result.data, result.numeric_tokens)
        return result

    async def match_open_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        amount: Decimal,
        currency: str,
        customer_id: UUID | None = None,
        reference_hint: str | None = None,
    ) -> ToolResult:
        target_amt = Decimal(str(amount)).quantize(_MONEY_Q, rounding=ROUND_HALF_UP)
        rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        rp.id AS rp_id,
                        rp.order_id,
                        o.order_no,
                        o.customer_id,
                        rp.milestone_label,
                        rp.due_amount,
                        rp.paid_amount,
                        (rp.due_amount - rp.paid_amount) AS outstanding_amount,
                        rp.currency,
                        rp.due_date,
                        rp.status
                    FROM receivables_payables rp
                    JOIN orders o ON o.id = rp.order_id AND o.tenant_id = rp.tenant_id
                    WHERE rp.tenant_id = :tid
                      AND rp.direction = 'receivable'
                      AND rp.status IN ('open', 'partial', 'overdue')
                      AND UPPER(rp.currency) = UPPER(:ccy)
                      AND (CAST(:cid AS uuid) IS NULL OR o.customer_id = CAST(:cid AS uuid))
                    ORDER BY ABS((rp.due_amount - rp.paid_amount) - :amt) ASC, rp.created_at
                        ASC, rp.id
                    LIMIT 10
                    """
                    ),
                    {
                        "tid": tenant_id,
                        "ccy": currency.upper(),
                        "cid": customer_id,
                        "amt": target_amt,
                    },
                )
            )
            .mappings()
            .all()
        )

        candidates: list[dict[str, Any]] = []
        for r in rows:
            outstanding = Decimal(str(r["outstanding_amount"])).quantize(
                _MONEY_Q, rounding=ROUND_HALF_UP
            )
            variance = (target_amt - outstanding).quantize(_MONEY_Q, rounding=ROUND_HALF_UP)
            hint_match = bool(
                reference_hint and str(r["order_no"]).lower() in reference_hint.lower()
            )
            candidates.append(
                {
                    "rp_id": str(r["rp_id"]),
                    "order_id": str(r["order_id"]),
                    "order_no": r["order_no"],
                    "milestone_label": r["milestone_label"],
                    "outstanding_amount": str(outstanding),
                    "variance_amount": str(variance),
                    "exact_amount_match": variance == Decimal("0.00"),
                    "reference_hint_match": hint_match,
                    "currency": r["currency"],
                    "status": r["status"],
                }
            )

        result = ToolResult(
            tool_name="match_open_receivables",
            ok=True,
            data={
                "received_amount": str(target_amt),
                "currency": currency.upper(),
                "candidates": candidates,
            },
        )
        result.data["source_tool_call_id"] = result.tool_call_id
        _extract_tool_tokens(result.data, result.numeric_tokens)
        return result

    async def query_readonly_analytics_sql(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        sql: str,
    ) -> ToolResult:
        is_safe, err_code, bounded_sql = validate_readonly_analytics_sql(sql)
        if not is_safe or bounded_sql is None:
            return ToolResult(
                tool_name="query_readonly_analytics_sql",
                ok=False,
                error_code=err_code,
                error_message="Analytics SQL failed read-only allowlist validation.",
            )

        try:
            async with session.begin_nested():
                previous = await session.scalar(text("SHOW statement_timeout"))
                await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                rows = (
                    (await session.execute(text(bounded_sql), {"tid": tenant_id})).mappings().all()
                )
                await session.execute(
                    text("SELECT set_config('statement_timeout', :prior, true)"),
                    {"prior": previous},
                )
        except DBAPIError:
            return self._failure(
                "query_readonly_analytics_sql",
                "ERR_ANALYTICS_QUERY_FAILED",
                "Analytics query could not be completed.",
            )
        serialized_rows: list[dict[str, Any]] = []
        for row in rows:
            clean_row: dict[str, Any] = {}
            for k, v in row.items():
                if isinstance(v, Decimal):
                    clean_row[k] = str(v)
                elif hasattr(v, "isoformat"):
                    clean_row[k] = v.isoformat()
                elif isinstance(v, UUID):
                    clean_row[k] = str(v)
                else:
                    clean_row[k] = v
            serialized_rows.append(clean_row)

        result = ToolResult(
            tool_name="query_readonly_analytics_sql",
            ok=True,
            data={
                "tenant_id": str(tenant_id),
                "row_count": len(serialized_rows),
                "rows": serialized_rows,
            },
        )
        result.data["source_tool_call_id"] = result.tool_call_id
        _extract_tool_tokens(result.data, result.numeric_tokens)
        return result
