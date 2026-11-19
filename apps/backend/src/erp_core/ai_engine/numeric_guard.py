from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from erp_core.ai_engine.tools import ToolResult

_NUMBER = r"-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_ID = r"[^\s,()。]+?"
_PRICE = re.compile(
    rf"For (?P<qty>{_NUMBER}) units of (?P<sku>{_ID}) \((?P<incoterm>[A-Z]+)\), "
    rf"the live unit price is \$(?P<price>{_NUMBER}) and we currently have "
    rf"(?P<stock>{_NUMBER}) units in stock\.?"
)
_PRICE_ZH = re.compile(
    rf"(?P<sku>{_ID})（(?P<incoterm>[A-Z]+)）：(?P<qty>{_NUMBER}) 件，"
    rf"单价 USD (?P<price>{_NUMBER})，库存 (?P<stock>{_NUMBER}) 件。?"
)
_TRACK = re.compile(rf"Order (?P<order_no>{_ID}) has tracking number (?P<tracking_number>{_ID})\.?")
_TRACK_ZH = re.compile(rf"订单 (?P<order_no>{_ID}) 的物流单号为 (?P<tracking_number>{_ID})。?")
_KINDS = {
    "price",
    "unit_price",
    "stock",
    "available_qty",
    "on_hand_qty",
    "reserved_qty",
    "qty",
    "quantity",
    "moq",
    "lead_time_days",
    "total_amount",
    "line_total",
    "retail_price_usd",
    "applied_unit_price_usd",
    "line_total_usd",
    "applied_moq_tier",
    "qty_requested",
    "estimated_margin_pct",
    "line_margin_pct",
    "fx_rate_snapshot",
    "unit_price_quote_ccy",
    "line_total_quote_ccy",
    "moq_tier_applied",
    "due_amount",
    "outstanding_amount",
    "variance_amount",
    "received_amount",
}
_IDENTIFIERS = {
    "sku",
    "spu_code",
    "currency",
    "incoterm",
    "order_no",
    "tracking_number",
    "eta",
    "estimated_delivery_at",
    "fulfillment_status",
    "standardized_status",
    "payment_status",
    "tracking_provider",
    "sub_status",
    "last_event_at",
}


class NumericGuardResult(BaseModel):
    passed: bool
    allow_l2_auto_send: bool
    extracted_claims: list[str] = Field(default_factory=list)
    ungrounded_tokens: list[str] = Field(default_factory=list)
    matched_tool_call_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


def _exact_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    raw = str(value)
    if len(raw) > 80 or not re.fullmatch(_NUMBER, raw):
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _trusted_rows(result: ToolResult) -> list[dict[str, Any]]:
    data = result.data
    match result.tool_name:
        case "get_live_sku_pricing_and_stock":
            return [
                {
                    **data,
                    "price": data.get("applied_unit_price_usd"),
                    "unit_price": data.get("applied_unit_price_usd"),
                    "stock": data.get("available_qty"),
                    "qty": data.get("qty_requested"),
                    "quantity": data.get("qty_requested"),
                    "moq": data.get("applied_moq_tier"),
                    "currency": "USD",
                    "line_total": data.get("line_total_usd"),
                }
            ]
        case "get_live_order_tracking":
            return [
                data,
                *(
                    {**data, **row, "eta": row.get("estimated_delivery_at")}
                    for row in data.get("shipments", [])
                ),
            ]
        case "calc_b2b_quote_matrix":
            # Never confuse quote totals with item prices or the order's maximum lead time
            # with a per-item lead time.
            return [
                data,
                *(
                    {
                        **row,
                        "currency": data.get("currency"),
                        "incoterm": data.get("incoterm"),
                        "price": row.get("unit_price_quote_ccy"),
                        "unit_price": row.get("unit_price_quote_ccy"),
                        "qty": row.get("quantity"),
                        "line_total": row.get("line_total_quote_ccy"),
                        "moq": row.get("moq_tier_applied"),
                    }
                    for row in data.get("items", [])
                ),
            ]
        case "match_open_receivables":
            return [data, *data.get("candidates", [])]
        case _:
            return []


def _has_exact_claim(row: dict[str, Any], key: str, offered: Any) -> bool:
    expected = row.get(key)
    if key in _KINDS:
        number = _exact_number(offered)
        return number is not None and number == _exact_number(expected)
    return key in _IDENTIFIERS and isinstance(offered, str) and offered == expected


class ToolGroundedNumericGuard:
    """Fail closed on unsupported prose; verify approved renderings against one SQL fact row.

    Free text is not a trusted representation of commercial claims: a parser that accepts
    arbitrary prose cannot tell whether every value was detected or assigned to the right field.
    Other phrasings go to human review. Structured claims require explicit call provenance and
    must all match the same result row, not unrelated values from several rows or calls.
    """

    def verify(
        self,
        reply_text: str,
        tool_results: list[ToolResult],
        structured_outputs: list[dict[str, Any]] | None = None,
    ) -> NumericGuardResult:
        trusted: dict[str, ToolResult] = {}
        duplicates: set[str] = set()
        for result in tool_results:
            source = result.tool_call_id
            if source in trusted:
                duplicates.add(source)
            if result.ok and result.data.get("source_tool_call_id") == source:
                trusted[source] = result
        for source in duplicates:
            trusted.pop(source, None)

        extracted: list[str] = []
        ungrounded: list[str] = []
        matched: set[str] = set()
        for output in structured_outputs or []:
            claim_source = output.get("source_tool_call_id")
            claim_result = trusted.get(claim_source) if isinstance(claim_source, str) else None
            claims = {key: value for key, value in output.items() if key != "source_tool_call_id"}
            extracted.extend(str(value) for value in claims.values())
            if claim_result is None:
                ungrounded.append("missing_or_invalid_source_tool_call_id")
            elif not claims or not any(
                all(_has_exact_claim(row, key, value) for key, value in claims.items())
                for row in _trusted_rows(claim_result)
            ):
                ungrounded.extend(str(value) for value in claims.values())
                if not claims:
                    ungrounded.append("missing_claims")
            else:
                matched.add(claim_result.tool_call_id)

        message = " ".join(reply_text.split())
        if message:
            for pattern, tool in (
                (_PRICE, "get_live_sku_pricing_and_stock"),
                (_PRICE_ZH, "get_live_sku_pricing_and_stock"),
                (_TRACK, "get_live_order_tracking"),
                (_TRACK_ZH, "get_live_order_tracking"),
            ):
                parsed = pattern.fullmatch(message)
                if parsed is None:
                    continue
                claims = parsed.groupdict()
                extracted.extend(claims.values())
                matching = [
                    source
                    for source, result in trusted.items()
                    if result.tool_name == tool
                    and any(
                        all(_has_exact_claim(row, key, value) for key, value in claims.items())
                        for row in _trusted_rows(result)
                    )
                ]
                if len(matching) == 1:
                    matched.add(matching[0])
                else:
                    ungrounded.extend(claims.values())
                break
            else:
                ungrounded.append("unsupported_reply_format")
        elif not structured_outputs:
            ungrounded.append("missing_claims")

        passed = not ungrounded and bool(matched)
        return NumericGuardResult(
            passed=passed,
            allow_l2_auto_send=passed,
            extracted_claims=extracted,
            ungrounded_tokens=ungrounded,
            matched_tool_call_ids=sorted(matched),
            reason=None if passed else "ungrounded_dynamic_numeric_claim",
        )
