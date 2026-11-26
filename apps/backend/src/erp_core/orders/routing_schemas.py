from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Carriers a waybill may be booked against (mirrors the DB CHECK constraint).
CarrierCode = Literal["yunexpress", "4px", "dhl", "ups", "fedex", "sandbox_carrier"]
#: Sandbox booking never calls a live carrier API; `live` requires credentials.
LabelMode = Literal["live", "sandbox"]


def _decimal_text(value: Any) -> str | None:
    """Canonical decimal string for JSONB, or ``None`` when absent/blank.

    Money and margin thresholds stay text end-to-end so the rules table never stores an
    IEEE-754 approximation of a financial boundary.
    """
    if value is None:
        return None
    text = format(value, "f") if isinstance(value, Decimal) else str(value).strip()
    if not text:
        return None
    try:
        return format(Decimal(text), "f")
    except ArithmeticError:
        return None


def _upper_list(values: list[str]) -> list[str]:
    return [value.strip().upper() for value in values if value.strip()]


class RoutingConditions(BaseModel):
    """Rule scope plus the anti-loss margin floor.

    Scope keys (``destination_countries``/``channel_types``/``order_modes``/
    ``min_total_amount``/``max_weight_kg``) decide whether the rule *matches*.
    ``min_margin_pct`` is the anti-loss floor: a matched rule whose floor is above the
    order's estimated margin holds the order for review.
    """

    destination_countries: list[str] = Field(default_factory=list)
    channel_types: list[str] = Field(default_factory=list)
    order_modes: list[str] = Field(default_factory=list)
    min_margin_pct: Decimal | None = None
    min_total_amount: Decimal | None = None
    max_weight_kg: Decimal | None = None

    @field_validator("destination_countries")
    @classmethod
    def _normalize_countries(cls, value: list[str]) -> list[str]:
        return _upper_list(value)

    @field_validator("channel_types", "order_modes")
    @classmethod
    def _normalize_tokens(cls, value: list[str]) -> list[str]:
        return [token.strip().lower() for token in value if token.strip()]

    def to_jsonb(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "destination_countries": list(self.destination_countries),
            "channel_types": list(self.channel_types),
            "order_modes": list(self.order_modes),
        }
        # Optional keys are written only when set so the stored JSONB stays compact.
        if self.min_margin_pct is not None:
            payload["min_margin_pct"] = _decimal_text(self.min_margin_pct)
        if self.min_total_amount is not None:
            payload["min_total_amount"] = _decimal_text(self.min_total_amount)
        if self.max_weight_kg is not None:
            payload["max_weight_kg"] = _decimal_text(self.max_weight_kg)
        return payload

    def is_empty_scope(self) -> bool:
        return not (
            self.destination_countries
            or self.channel_types
            or self.order_modes
            or self.min_total_amount is not None
            or self.max_weight_kg is not None
        )


class RoutingActions(BaseModel):
    """What a matched rule does to the order."""

    warehouse_id: UUID | None = None
    carrier_code: str | None = None
    service_code: str | None = None
    add_tags: list[str] = Field(default_factory=list)
    hold_for_review: bool = False
    hold_reason: str | None = None

    @field_validator("carrier_code", "service_code", "hold_reason")
    @classmethod
    def _strip_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("add_tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        return [tag.strip() for tag in value if tag.strip()]

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "warehouse_id": str(self.warehouse_id) if self.warehouse_id else None,
            "carrier_code": self.carrier_code,
            "service_code": self.service_code,
            "add_tags": list(self.add_tags),
            "hold_for_review": self.hold_for_review,
            "hold_reason": self.hold_reason,
        }


class RoutingRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(default=100, ge=0, le=100_000)
    is_enabled: bool = True
    conditions: RoutingConditions = Field(default_factory=RoutingConditions)
    actions: RoutingActions = Field(default_factory=RoutingActions)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Rule name must not be blank.")
        return stripped

    @model_validator(mode="after")
    def _reject_empty_rule(self) -> Self:
        if self.conditions.is_empty_scope() and self.conditions.min_margin_pct is None:
            raise ValueError(
                "A routing rule needs at least one condition (destination_countries, "
                "channel_types, order_modes, min_total_amount, max_weight_kg) or a "
                "min_margin_pct anti-loss floor."
            )
        return self


class RoutingRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    priority: int | None = Field(default=None, ge=0, le=100_000)
    is_enabled: bool | None = None
    conditions: RoutingConditions | None = None
    actions: RoutingActions | None = None

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Rule name must not be blank.")
        return stripped


class RoutingRuleRead(BaseModel):
    id: UUID
    name: str
    priority: int
    is_enabled: bool
    conditions: RoutingConditions
    actions: RoutingActions
    created_at: datetime
    updated_at: datetime


class OrderRoutingEvaluationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_id: UUID
    matched_rule_ids: list[str]
    recommended_warehouse_id: UUID | None
    recommended_carrier: str | None
    recommended_service: str | None
    estimated_margin_pct: Decimal | None
    is_held: bool
    hold_reason: str | None
    hold_released_by: UUID | None
    hold_released_at: datetime | None
    applied_tags: list[str]
    evaluated_at: datetime


class ReleaseHoldRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 3:
            raise ValueError("A hold release reason of at least 3 characters is required.")
        return stripped


class ShippingLabelBookRequest(BaseModel):
    """Book a carrier waybill for an order and persist its 4x6 thermal PDF."""

    carrier_code: CarrierCode = "sandbox_carrier"
    service_code: str | None = Field(default=None, max_length=64)
    mode: LabelMode = "sandbox"
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    #: Manager-only escape hatch for a routing hold; the role is re-checked server-side.
    override_hold: bool = False
    override_reason: str | None = Field(default=None, max_length=500)
    #: Optional carrier-declared weight; defaults to the order's SKU weights in kg.
    declared_weight_kg: Decimal | None = Field(default=None, gt=Decimal("0"))

    @model_validator(mode="after")
    def _override_needs_reason(self) -> Self:
        if self.override_hold:
            reason = (self.override_reason or "").strip()
            if len(reason) < 3:
                raise ValueError(
                    "override_reason of at least 3 characters is required when "
                    "override_hold is true."
                )
            self.override_reason = reason
        if self.currency is not None:
            self.currency = self.currency.upper()
        return self


class CarrierShippingLabelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_id: UUID
    shipment_id: UUID | None
    carrier_code: CarrierCode
    service_code: str
    tracking_number: str
    waybill_number: str
    label_format: str
    label_r2_key: str | None
    mode: LabelMode
    shipping_cost_estimate: Decimal | None
    currency: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Batch waybills (up to 50 orders) + merged 4x6 thermal PDF
# ---------------------------------------------------------------------------

#: Upper bound on one batch call. A warehouse picks a print run, never the whole table.
BATCH_LABEL_LIMIT = 50

BatchSkipCode = Literal[
    "order_not_found",
    "order_inactive",
    "held_for_review",
    "balance_unpaid",
    "booking_failed",
]


class BatchShippingLabelRequest(BaseModel):
    """Book (or reuse) one waybill per order in a single warehouse print run."""

    order_ids: list[UUID] = Field(min_length=1, max_length=BATCH_LABEL_LIMIT)
    carrier_code: CarrierCode = "sandbox_carrier"
    mode: LabelMode = "sandbox"
    service_code: str | None = Field(default=None, max_length=64)

    @field_validator("order_ids")
    @classmethod
    def _dedupe_order_ids(cls, value: list[UUID]) -> list[UUID]:
        seen: dict[UUID, None] = {}
        for order_id in value:
            seen.setdefault(order_id, None)
        return list(seen)

    @field_validator("service_code")
    @classmethod
    def _strip_service_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class BatchShippingLabelBookedItem(BaseModel):
    order_id: UUID
    order_no: str
    label_id: UUID
    tracking_number: str
    waybill_number: str
    carrier_code: CarrierCode
    service_code: str
    mode: LabelMode
    #: ``True`` when an already-booked waybill was reused instead of buying a new one.
    reused: bool
    shipping_cost_estimate: Decimal | None
    currency: str


class BatchShippingLabelSkippedItem(BaseModel):
    order_id: UUID
    order_no: str | None = None
    reason_code: BatchSkipCode
    reason: str


class BatchShippingLabelResponse(BaseModel):
    booked: list[BatchShippingLabelBookedItem] = Field(default_factory=list)
    skipped: list[BatchShippingLabelSkippedItem] = Field(default_factory=list)


class BatchLabelPdfRequest(BaseModel):
    """Merge 4x6 label PDFs by label id and/or by order (latest label wins)."""

    label_ids: list[UUID] = Field(default_factory=list, max_length=BATCH_LABEL_LIMIT)
    order_ids: list[UUID] = Field(default_factory=list, max_length=BATCH_LABEL_LIMIT)

    @model_validator(mode="after")
    def _require_a_selection(self) -> Self:
        if not self.label_ids and not self.order_ids:
            raise ValueError("Send label_ids, order_ids, or both.")
        if len(self.label_ids) + len(self.order_ids) > BATCH_LABEL_LIMIT:
            raise ValueError(
                f"At most {BATCH_LABEL_LIMIT} labels may be merged in one call."
            )
        return self


class ScanDispatchRequest(BaseModel):
    """A warehouse barcode: tracking number, shipment tracking number or order no."""

    code: str = Field(min_length=1, max_length=128)

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A scanned code is required.")
        return stripped


class ScanDispatchResponse(BaseModel):
    order_id: UUID
    order_no: str
    tracking_number: str | None = None
    status: str
    dispatched_at: datetime
