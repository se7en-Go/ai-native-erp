"""Carrier waybill booking + genuine 4x6 inch thermal label rendering.

Three adapter implementations share one `CarrierLabelAdapter` protocol:

* `SandboxCarrierAdapter` - deterministic waybill/tracking numbers, no network.
* `YunExpressAdapter` - YunExpress v1 order-creation contract with the
  `Authorization: Basic base64(customer_code & api_secret)` header.
* `FourPxAdapter` - 4PX open-platform `fu.wms.outbound.create` contract with an
  MD5 request signature over the sorted parameter block.

Live credentials are read from the injected request/env only, are never logged, and
are never echoed back in a response or error detail. When a carrier returns only a
tracking number, the adapter renders the same deterministic 288x432 pt (4x6 inch)
`reportlab` waybill locally so the warehouse always gets a printable label.

All money stays `decimal.Decimal` (`ROUND_HALF_UP`) - never float.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx
from reportlab.graphics.barcode import code128  # type: ignore[import-untyped]
from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.orders.models import Order, Shipment
from erp_core.orders.routing_models import CarrierShippingLabel
from erp_core.orders.routing_schemas import CarrierShippingLabelRead, ShippingLabelBookRequest
from erp_core.orders.routing_service import RoutingService
from erp_core.orders.tracking_adapter import Tracking17TrackAdapter
from erp_core.shared.enums import (
    FulfillmentStatus,
    OrderMode,
    PaymentStatus,
    TrackingProvider,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

logger = logging.getLogger(__name__)

#: Exactly 4x6 inch thermal stock at 72 pt/inch.
THERMAL_LABEL_WIDTH_PT = 288
THERMAL_LABEL_HEIGHT_PT = 432

_CENT = Decimal("0.01")
_KG_QUANTUM = Decimal("0.001")
_MANAGER_ROLES = frozenset({UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER})
_BALANCE_OVERRIDE_ROLES = frozenset({UserRole.OWNER, UserRole.FINANCE})

#: Per-carrier `(base_fee, per_kg_fee)` used for the deterministic pre-booking estimate.
#: Live carriers replace this with their own rate card on the response; the estimate is
#: what the operator sees *before* committing money.
CARRIER_RATE_CARDS: dict[str, tuple[Decimal, Decimal]] = {
    "yunexpress": (Decimal("3.20"), Decimal("4.60")),
    "4px": (Decimal("3.05"), Decimal("4.35")),
    "dhl": (Decimal("9.50"), Decimal("7.20")),
    "ups": (Decimal("9.20"), Decimal("7.05")),
    "fedex": (Decimal("9.10"), Decimal("6.95")),
    "sandbox_carrier": (Decimal("2.50"), Decimal("4.00")),
}

#: Default service code per carrier when the request does not pin one.
DEFAULT_SERVICE_CODES: dict[str, str] = {
    "yunexpress": "YunExpress-Expedited",
    "4px": "4PX-Standard",
    "dhl": "DHL-Express-Worldwide",
    "ups": "UPS-Worldwide-Saver",
    "fedex": "FedEx-International-Priority",
    "sandbox_carrier": "Sandbox-Standard",
}

#: Carriers whose live booking contract is wired in this deployment. Anything else may
#: only be booked in sandbox mode - the engine never pretends a real waybill exists.
LIVE_CARRIER_CODES = frozenset({"yunexpress", "4px"})

CARRIER_DISPLAY_NAMES: dict[str, str] = {
    "yunexpress": "YunExpress",
    "4px": "4PX",
    "dhl": "DHL Express",
    "ups": "UPS",
    "fedex": "FedEx",
    "sandbox_carrier": "Sandbox Carrier",
}


def estimate_shipping_cost(carrier_code: str, weight_kg: Decimal) -> Decimal:
    """Deterministic, Decimal-exact shipping cost estimate for a parcel weight."""
    base, per_kg = CARRIER_RATE_CARDS.get(
        carrier_code, CARRIER_RATE_CARDS["sandbox_carrier"]
    )
    billable = max(weight_kg, Decimal("0.001")).quantize(_KG_QUANTUM, rounding=ROUND_HALF_UP)
    return (base + per_kg * billable).quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LabelLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class CarrierBookingRequest:
    """Carrier-agnostic booking input; carries no secrets in ``repr``."""

    tenant_id: UUID
    order_id: UUID
    order_no: str
    carrier_code: str
    service_code: str
    mode: str
    destination_country: str
    destination_city: str
    recipient_name: str
    declared_weight_kg: Decimal
    currency: str
    lines: tuple[LabelLine, ...]
    sequence: int = 0
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CarrierBookingResult:
    """What a carrier returned, plus the printable 4x6 PDF bytes."""

    tracking_number: str
    waybill_number: str
    shipping_cost_estimate: Decimal
    currency: str
    raw_response: dict[str, Any]
    pdf_bytes: bytes


@runtime_checkable
class CarrierLabelAdapter(Protocol):
    """Carrier waybill provider contract."""

    carrier_code: str

    async def book(self, request: CarrierBookingRequest) -> CarrierBookingResult: ...


# ---------------------------------------------------------------------------
# 4x6 thermal label rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThermalLabelContext:
    carrier_code: str
    service_code: str
    order_no: str
    tracking_number: str
    waybill_number: str
    destination_country: str
    destination_city: str
    recipient_name: str
    weight_kg: Decimal
    currency: str
    shipping_cost_estimate: Decimal | None
    lines: tuple[LabelLine, ...]
    mode: str


def _barcode(value: str) -> code128.Code128:
    return code128.Code128(value, barWidth=0.9, barHeight=44, humanReadable=False)


def render_thermal_label_pdf(ctx: ThermalLabelContext) -> bytes:
    """Render a genuine 4x6 inch (288x432 pt) thermal waybill PDF.

    Deterministic by construction: `invariant=1` pins the PDF header/date so the same
    label always re-renders to byte-identical output (safe for S3 dedupe and tests).
    """
    buffer = BytesIO()
    pdf = canvas.Canvas(
        buffer, pagesize=(THERMAL_LABEL_WIDTH_PT, THERMAL_LABEL_HEIGHT_PT), invariant=1
    )
    width, height = THERMAL_LABEL_WIDTH_PT, THERMAL_LABEL_HEIGHT_PT
    ink = colors.HexColor("#111827")
    accent = colors.HexColor("#1d4ed8")
    rule = colors.HexColor("#d1d5db")

    # Header band -------------------------------------------------------------
    pdf.setFillColor(accent)
    pdf.rect(0, height - 46, width, 46, stroke=0, fill=1)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(12, height - 24, CARRIER_DISPLAY_NAMES.get(ctx.carrier_code, ctx.carrier_code))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(12, height - 36, ctx.service_code[:52])
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawRightString(width - 12, height - 24, "4x6 THERMAL")
    pdf.setFont("Helvetica", 7)
    pdf.drawRightString(width - 12, height - 36, f"MODE {ctx.mode.upper()}")
    pdf.setFillColor(ink)

    y = height - 60
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(12, y, "WAYBILL")
    pdf.setFont("Courier-Bold", 11)
    pdf.drawString(70, y, ctx.waybill_number[:26])

    y -= 18
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(12, y, "TRACKING")
    pdf.setFont("Courier-Bold", 11)
    pdf.drawString(70, y, ctx.tracking_number[:26])

    # Barcodes ---------------------------------------------------------------
    _barcode(ctx.waybill_number).drawOn(pdf, 12, y - 62)
    _barcode(ctx.tracking_number).drawOn(pdf, 148, y - 62)
    pdf.setFont("Courier", 6)
    pdf.drawString(12, y - 72, ctx.waybill_number[:34])
    pdf.drawString(148, y - 72, ctx.tracking_number[:34])

    # Order / destination block ---------------------------------------------
    y = y - 90
    pdf.setStrokeColor(rule)
    pdf.setLineWidth(0.6)
    pdf.line(12, y + 12, width - 12, y + 12)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(12, y - 2, "ORDER")
    pdf.setFont("Courier-Bold", 9)
    pdf.drawString(52, y - 2, ctx.order_no[:28])
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(12, y - 14, "TO")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(52, y - 14, (ctx.recipient_name or "CONSIGNEE")[:34])
    pdf.setFont("Helvetica", 8)
    pdf.drawString(
        52,
        y - 26,
        f"{ctx.destination_city or '-'} / {ctx.destination_country or '--'}",
    )
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(12, y - 38, "WEIGHT")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(56, y - 38, f"{ctx.weight_kg:.3f} kg")
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(150, y - 38, "FREIGHT")
    pdf.setFont("Helvetica", 8)
    estimate = (
        f"{ctx.shipping_cost_estimate:.2f} {ctx.currency}"
        if ctx.shipping_cost_estimate is not None
        else f"- {ctx.currency}"
    )
    pdf.drawString(194, y - 38, estimate)

    # SKU / quantity table ---------------------------------------------------
    y -= 54
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawString(12, y, "SKU")
    pdf.drawRightString(width - 12, y, "QTY")
    pdf.setStrokeColor(rule)
    pdf.line(12, y - 3, width - 12, y - 3)
    y -= 15
    pdf.setFont("Courier", 7)
    for line in ctx.lines[:9]:
        pdf.drawString(12, y, line.sku[:38])
        pdf.drawRightString(width - 12, y, str(line.quantity))
        y -= 11
    if len(ctx.lines) > 9:
        pdf.setFont("Courier-Oblique", 6.5)
        pdf.drawString(12, y, f"+{len(ctx.lines) - 9} more SKU lines")

    pdf.setStrokeColor(rule)
    pdf.line(12, 26, width - 12, 26)
    pdf.setFont("Helvetica", 6)
    pdf.setFillColor(colors.HexColor("#6b7280"))
    pdf.drawString(12, 16, "Generated by ai-native-erp order routing engine")
    pdf.drawRightString(width - 12, 16, f"288x432pt · {ctx.order_no[:20]}")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _deterministic_digits(seed: str, length: int) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    digits = "".join(char for char in digest if char.isdigit())
    while len(digits) < length:  # pragma: no cover - defensive, digest always long enough
        digits += hashlib.sha256(digits.encode("utf-8")).hexdigest()
    return digits[:length]


class SandboxCarrierAdapter:
    """No-network carrier used for sandbox books and deterministic tests."""

    carrier_code = "sandbox_carrier"

    async def book(self, request: CarrierBookingRequest) -> CarrierBookingResult:
        seed = (
            f"{request.tenant_id}:{request.order_no}:{request.carrier_code}:"
            f"{request.service_code}:{request.sequence}"
        )
        waybill = f"SBX{_deterministic_digits(seed, 12)}"
        tracking = f"SBX{_deterministic_digits(seed + ':t', 9)}CN"
        cost = estimate_shipping_cost(request.carrier_code, request.declared_weight_kg)
        ctx = ThermalLabelContext(
            carrier_code=request.carrier_code,
            service_code=request.service_code,
            order_no=request.order_no,
            tracking_number=tracking,
            waybill_number=waybill,
            destination_country=request.destination_country,
            destination_city=request.destination_city,
            recipient_name=request.recipient_name,
            weight_kg=request.declared_weight_kg,
            currency=request.currency,
            shipping_cost_estimate=cost,
            lines=request.lines,
            mode=request.mode,
        )
        return CarrierBookingResult(
            tracking_number=tracking,
            waybill_number=waybill,
            shipping_cost_estimate=cost,
            currency=request.currency,
            raw_response={
                "carrier": "sandbox_carrier",
                "accepted": True,
                "order_number": request.order_no,
                "waybill_number": waybill,
                "tracking_number": tracking,
            },
            pdf_bytes=render_thermal_label_pdf(ctx),
        )


def _upstream_error(carrier: str, detail: str) -> AppError:
    """Structured 502 that never leaks credentials, headers or the raw upstream body."""
    return AppError(
        slug=f"{carrier}-booking-failed",
        status_code=502,
        code="ERR_CARRIER_UPSTREAM",
        title=f"{CARRIER_DISPLAY_NAMES.get(carrier, carrier)} Booking Failed",
        detail=detail,
    )


class YunExpressAdapter:
    """YunExpress v1 waybill contract (`POST /lms/api/waybill`).

    Authentication is HTTP Basic over ``"{customer_code}&{api_secret}"``; the header is
    built per call, never stored on the instance and never written to logs.
    """

    carrier_code = "yunexpress"
    default_base_url = "https://api.yunexpress.com/lms/api/waybill"

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client = http_client
        self._base_url = (
            base_url or os.getenv("YUNEXPRESS_API_URL") or self.default_base_url
        )

    @staticmethod
    def build_auth_header(customer_code: str, api_secret: str) -> str:
        raw = f"{customer_code}&{api_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def build_payload(self, request: CarrierBookingRequest) -> dict[str, Any]:
        """:returns: the YunExpress v1 order-creation JSON body."""
        parcels = [
            {"SKU": line.sku, "Quantity": line.quantity} for line in request.lines
        ]
        return {
            "customerCode": request.credentials.get("customer_code", ""),
            "command": 0,
            "orderNumber": request.order_no,
            "shippingMethodCode": request.service_code,
            "shippingDate": None,
            "shipTo": {
                "CountryCode": request.destination_country,
                "City": request.destination_city,
                "Name": request.recipient_name,
            },
            "parcels": parcels,
            "weight": str(request.declared_weight_kg),
            "currencyCode": request.currency,
            "labelFormat": "PDF",
            "labelSize": "4x6",
        }

    async def book(self, request: CarrierBookingRequest) -> CarrierBookingResult:
        customer_code = request.credentials.get("customer_code") or os.getenv(
            "YUNEXPRESS_CUSTOMER_CODE", ""
        )
        api_secret = request.credentials.get("api_secret") or os.getenv(
            "YUNEXPRESS_API_SECRET", ""
        )
        if not customer_code or not api_secret:
            raise _upstream_error(
                "yunexpress",
                "YunExpress live credentials (YUNEXPRESS_CUSTOMER_CODE / "
                "YUNEXPRESS_API_SECRET) are not configured.",
            )
        headers = {
            "Authorization": self.build_auth_header(customer_code, api_secret),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = self.build_payload(request)
        try:
            if self._client is not None:
                response = await self._client.post(self._base_url, headers=headers, json=payload)
            else:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(self._base_url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Only the exception type is surfaced: request headers carry the secret pair.
            logger.warning("yunexpress booking failed: %s", type(exc).__name__)
            raise _upstream_error(
                "yunexpress", "YunExpress did not accept the waybill request."
            ) from exc

        parsed = self._parse(body)
        if parsed is None:
            raise _upstream_error(
                "yunexpress", "YunExpress response did not contain a waybill number."
            )
        waybill, tracking, cost = parsed
        ctx = ThermalLabelContext(
            carrier_code=request.carrier_code,
            service_code=request.service_code,
            order_no=request.order_no,
            tracking_number=tracking,
            waybill_number=waybill,
            destination_country=request.destination_country,
            destination_city=request.destination_city,
            recipient_name=request.recipient_name,
            weight_kg=request.declared_weight_kg,
            currency=request.currency,
            shipping_cost_estimate=cost,
            lines=request.lines,
            mode=request.mode,
        )
        return CarrierBookingResult(
            tracking_number=tracking,
            waybill_number=waybill,
            shipping_cost_estimate=cost,
            currency=request.currency,
            raw_response=body if isinstance(body, dict) else {"raw": body},
            pdf_bytes=render_thermal_label_pdf(ctx),
        )

    @staticmethod
    def _parse(body: Any) -> tuple[str, str, Decimal] | None:
        """Extract ``(waybill, tracking, cost)`` from a YunExpress envelope."""
        if not isinstance(body, dict):
            return None
        result = body.get("Result") if isinstance(body.get("Result"), dict) else body
        if not isinstance(result, dict):
            return None
        if result.get("Success") is False:
            return None
        waybill = result.get("WayBillNumber") or result.get("waybill_number")
        tracking = result.get("TrackingNumber") or result.get("tracking_number") or waybill
        if not isinstance(waybill, str) or not waybill.strip():
            return None
        if not isinstance(tracking, str) or not tracking.strip():
            tracking = waybill
        raw_cost = result.get("ShippingCost") or result.get("shipping_cost")
        cost = (
            Decimal(str(raw_cost)).quantize(_CENT, rounding=ROUND_HALF_UP)
            if raw_cost is not None
            else Decimal("0.00")
        )
        return waybill.strip(), tracking.strip(), cost


class FourPxAdapter:
    """4PX open-platform outbound creation (`fu.wms.outbound.create`).

    The signature is the upper-case MD5 of ``app_secret + sorted(key+value) + app_secret``
    over the non-empty request parameters, which is the documented 4PX partner scheme.
    """

    carrier_code = "4px"
    default_base_url = "https://openapi.4px.com/router/api/service"

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client = http_client
        self._base_url = base_url or os.getenv("FOURPX_API_URL") or self.default_base_url

    @staticmethod
    def sign(params: Mapping[str, str], app_secret: str) -> str:
        ordered = "".join(f"{key}{params[key]}" for key in sorted(params) if params[key])
        return hashlib.md5(  # noqa: S324 - carrier-mandated MD5 signature, not a password hash
            f"{app_secret}{ordered}{app_secret}".encode()
        ).hexdigest().upper()

    def build_payload(
        self, request: CarrierBookingRequest, credentials: Mapping[str, str]
    ) -> dict[str, str]:
        app_key = credentials.get("app_key", "")
        app_secret = credentials.get("app_secret", "")
        parcel = {
            "logistics_product_code": request.service_code,
            "ref_no": request.order_no,
            "to_country_code": request.destination_country,
            "to_city": request.destination_city,
            "to_contact_name": request.recipient_name,
            "weight": str(request.declared_weight_kg),
            "currency": request.currency,
            "label_format": "PDF_4X6",
            "items": [
                {"sku_code": line.sku, "qty": line.quantity} for line in request.lines
            ],
        }
        params = {
            "app_key": app_key,
            "format": "json",
            "method": "fu.wms.outbound.create",
            "timestamp": "0",
            "v": "1.0",
            "data": json.dumps(parcel, separators=(",", ":"), sort_keys=True),
        }
        params["sign"] = self.sign(params, app_secret)
        return params

    async def book(self, request: CarrierBookingRequest) -> CarrierBookingResult:
        app_key = request.credentials.get("app_key") or os.getenv("FOURPX_APP_KEY", "")
        app_secret = request.credentials.get("app_secret") or os.getenv(
            "FOURPX_APP_SECRET", ""
        )
        if not app_key or not app_secret:
            raise _upstream_error(
                "4px",
                "4PX live credentials (FOURPX_APP_KEY / FOURPX_APP_SECRET) are not configured.",
            )
        params = self.build_payload(request, {"app_key": app_key, "app_secret": app_secret})
        try:
            if self._client is not None:
                response = await self._client.post(self._base_url, data=params)
            else:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(self._base_url, data=params)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("4px booking failed: %s", type(exc).__name__)
            raise _upstream_error("4px", "4PX did not accept the waybill request.") from exc

        parsed = self._parse(body)
        if parsed is None:
            raise _upstream_error("4px", "4PX response did not contain a tracking number.")
        waybill, tracking, cost = parsed
        ctx = ThermalLabelContext(
            carrier_code=request.carrier_code,
            service_code=request.service_code,
            order_no=request.order_no,
            tracking_number=tracking,
            waybill_number=waybill,
            destination_country=request.destination_country,
            destination_city=request.destination_city,
            recipient_name=request.recipient_name,
            weight_kg=request.declared_weight_kg,
            currency=request.currency,
            shipping_cost_estimate=cost,
            lines=request.lines,
            mode=request.mode,
        )
        return CarrierBookingResult(
            tracking_number=tracking,
            waybill_number=waybill,
            shipping_cost_estimate=cost,
            currency=request.currency,
            raw_response=body if isinstance(body, dict) else {"raw": body},
            pdf_bytes=render_thermal_label_pdf(ctx),
        )

    @staticmethod
    def _parse(body: Any) -> tuple[str, str, Decimal] | None:
        if not isinstance(body, dict):
            return None
        if str(body.get("resultCode", body.get("result_code", "1"))) not in {"1", "0"}:
            return None
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        if not isinstance(data, dict):
            return None
        tracking = data.get("tracking_no") or data.get("trackingNo") or data.get("ref_no")
        waybill = data.get("waybill_no") or data.get("waybillNo") or tracking
        if not isinstance(tracking, str) or not tracking.strip():
            return None
        if not isinstance(waybill, str) or not waybill.strip():
            waybill = tracking
        raw_cost = data.get("freight") or data.get("shipping_cost")
        cost = (
            Decimal(str(raw_cost)).quantize(_CENT, rounding=ROUND_HALF_UP)
            if raw_cost is not None
            else Decimal("0.00")
        )
        return waybill.strip(), tracking.strip(), cost


def build_adapter(
    carrier_code: str,
    http_client: httpx.AsyncClient | None = None,
) -> CarrierLabelAdapter:
    """Resolve the adapter for a carrier code; unknown codes are a 422."""
    if carrier_code == "yunexpress":
        return YunExpressAdapter(http_client=http_client)
    if carrier_code == "4px":
        return FourPxAdapter(http_client=http_client)
    if carrier_code in {"sandbox_carrier", "dhl", "ups", "fedex"}:
        # Sandbox mode renders deterministic labels for every carrier family; only
        # YunExpress and 4PX have live booking contracts wired in this track.
        return SandboxCarrierAdapter()
    raise AppError(
        slug="unsupported-carrier",
        status_code=422,
        code="ERR_UNSUPPORTED_CARRIER",
        title="Unsupported Carrier",
        detail=f"Carrier '{carrier_code}' cannot be booked by this deployment.",
    )


# ---------------------------------------------------------------------------
# Booking orchestration
# ---------------------------------------------------------------------------


async def _order_lines(
    session: AsyncSession, tenant_id: UUID, order_id: UUID
) -> tuple[LabelLine, ...]:
    rows = (
        await session.execute(
            text(
                "SELECT i.sku_snapshot, i.quantity FROM order_items i "
                "WHERE i.tenant_id = :tid AND i.order_id = :oid "
                "ORDER BY i.sku_snapshot ASC"
            ),
            {"tid": tenant_id, "oid": order_id},
        )
    ).all()
    return tuple(LabelLine(sku=str(row[0]), quantity=int(row[1])) for row in rows)


async def _order_weight_kg(session: AsyncSession, tenant_id: UUID, order_id: UUID) -> Decimal:
    grams = await session.scalar(
        text(
            "SELECT COALESCE(SUM(v.weight_grams * i.quantity), 0) "
            "FROM order_items i "
            "JOIN product_variants v ON v.id = i.variant_id AND v.tenant_id = i.tenant_id "
            "WHERE i.tenant_id = :tid AND i.order_id = :oid"
        ),
        {"tid": tenant_id, "oid": order_id},
    )
    weight = (Decimal(str(grams or 0)) / Decimal(1000)).quantize(
        _KG_QUANTUM, rounding=ROUND_HALF_UP
    )
    return max(weight, Decimal("0.001"))


async def _destination(
    session: AsyncSession, tenant_id: UUID, order: Order
) -> tuple[str, str, str]:
    metadata: dict[str, Any] = order.channel_metadata or {}
    country: str | None = None
    for key in ("destination_country", "shipping_country", "country_code"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            country = raw.strip().upper()[:2]
            break
    city = ""
    recipient = ""
    shipping = metadata.get("shipping_address")
    if isinstance(shipping, dict):
        raw_city = shipping.get("city")
        if isinstance(raw_city, str):
            city = raw_city
        raw_recipient = shipping.get("name") or shipping.get("recipient")
        if isinstance(raw_recipient, str):
            recipient = raw_recipient
        raw_country = shipping.get("country") or shipping.get("country_code")
        if country is None and isinstance(raw_country, str) and raw_country.strip():
            country = raw_country.strip().upper()[:2]
    customer_row = (
        await session.execute(
            text(
                "SELECT country_code, display_name FROM customers "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {"tid": tenant_id, "cid": order.customer_id},
        )
    ).first()
    if customer_row is not None:
        if country is None and isinstance(customer_row[0], str) and customer_row[0].strip():
            country = customer_row[0].strip().upper()[:2]
        if not recipient and isinstance(customer_row[1], str):
            recipient = customer_row[1]
    return country or "--", city, recipient


async def _b2b_balance_gate(
    session: AsyncSession, tenant_id: UUID, order: Order, principal: CurrentPrincipal
) -> None:
    """A wholesale waybill may not be bought while the 70% balance is unsettled."""
    if order.order_mode != OrderMode.B2B_WHOLESALE:
        return
    unsettled = int(
        (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM receivables_payables "
                    "WHERE tenant_id = :tid AND order_id = :oid "
                    "  AND direction = 'receivable' "
                    "  AND status IN ('open', 'partial', 'overdue')"
                ),
                {"tid": tenant_id, "oid": order.id},
            )
        ).scalar_one()
    )
    if order.payment_status == PaymentStatus.PAID and unsettled == 0:
        return
    raise AppError(
        slug="balance-payment-required",
        status_code=409,
        code="ERR_BALANCE_PAYMENT_REQUIRED",
        title="Balance Payment Required",
        detail=(
            "A B2B wholesale waybill cannot be booked until the 70% balance receivable "
            "is settled"
            + (
                " (Owner/Finance override is available on the shipment flow)."
                if principal.role in _BALANCE_OVERRIDE_ROLES
                else "."
            )
        ),
    )


async def _persist_label_pdf(
    storage: ObjectStorageService | None,
    key: str,
    pdf_bytes: bytes,
) -> str | None:
    """Upload the label PDF; a storage outage must never lose a booked waybill."""
    if storage is None:
        return None
    try:
        await storage.upload_bytes(key, pdf_bytes, content_type="application/pdf")
    except Exception as exc:  # noqa: BLE001 - S3/MinIO outage is explicitly tolerated
        logger.warning(
            "label PDF upload skipped (%s); serving re-rendered label", type(exc).__name__
        )
        return None
    return key


async def book_shipping_label(
    session: AsyncSession,
    storage: ObjectStorageService | None,
    tenant_id: UUID,
    principal: CurrentPrincipal,
    order_id: UUID,
    req: ShippingLabelBookRequest,
    *,
    http_client: httpx.AsyncClient | None = None,
    tracking_adapter: Tracking17TrackAdapter | None = None,
    credentials: Mapping[str, str] | None = None,
) -> tuple[CarrierShippingLabelRead, bytes]:
    """Book a carrier waybill, persist the 4x6 PDF, and record the shipment.

    Refuses with HTTP 409 ``ERR_ORDER_HELD_FOR_REVIEW`` while the latest routing
    evaluation holds the order, unless a Manager+ passes ``override_hold`` with a reason.
    """
    routing = RoutingService()
    order = (
        await session.execute(
            select(Order)
            .where(Order.tenant_id == tenant_id, Order.id == order_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if order is None:
        raise AppError(
            slug="order-not-found",
            status_code=404,
            code="ERR_ORDER_NOT_FOUND",
            title="Order Not Found",
            detail=f"Order '{order_id}' not found.",
        )
    if order.fulfillment_status in {FulfillmentStatus.CANCELLED, FulfillmentStatus.REFUNDED}:
        raise AppError(
            slug="order-inactive",
            status_code=409,
            code="ERR_ORDER_INACTIVE",
            title="Order Inactive",
            detail="Cannot book a waybill for a cancelled/refunded order.",
        )

    evaluation = await routing.latest_evaluation(session, tenant_id, order.id)
    if evaluation is not None and evaluation.is_held:
        if not req.override_hold:
            raise AppError(
                slug="order-held-for-review",
                status_code=409,
                code="ERR_ORDER_HELD_FOR_REVIEW",
                title="Order Held For Review",
                detail=(
                    f"Routing evaluation held order '{order.order_no}': "
                    f"{evaluation.hold_reason or 'anti-loss hold'}. "
                    "A Manager must release the hold before booking a waybill."
                ),
            )
        if principal.role not in _MANAGER_ROLES:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Hold Override Forbidden",
                detail="Only Owner, Admin or Manager may override a routing hold.",
            )

    await _b2b_balance_gate(session, tenant_id, order, principal)

    carrier_code = req.carrier_code
    if req.mode == "live" and carrier_code == "sandbox_carrier":
        raise AppError(
            slug="sandbox-carrier-live-mode",
            status_code=422,
            code="ERR_SANDBOX_CARRIER_LIVE_MODE",
            title="Sandbox Carrier Cannot Book Live",
            detail=(
                "The sandbox carrier never calls a real carrier API. Pick YunExpress or "
                "4PX for a live waybill, or switch the mode to sandbox."
            ),
        )
    if req.mode == "live" and carrier_code not in LIVE_CARRIER_CODES:
        raise AppError(
            slug="carrier-live-mode-unsupported",
            status_code=422,
            code="ERR_CARRIER_LIVE_MODE_UNSUPPORTED",
            title="Live Booking Not Wired For This Carrier",
            detail=(
                f"'{carrier_code}' has no live booking contract in this deployment yet. "
                "Book it in sandbox mode, or use YunExpress / 4PX for live waybills."
            ),
        )

    service_code = req.service_code or DEFAULT_SERVICE_CODES.get(
        carrier_code, "Standard"
    )
    country, city, recipient = await _destination(session, tenant_id, order)
    lines = await _order_lines(session, tenant_id, order.id)
    weight_kg = req.declared_weight_kg or await _order_weight_kg(
        session, tenant_id, order.id
    )
    sequence = int(
        await session.scalar(
            select(func.count())
            .select_from(CarrierShippingLabel)
            .where(
                CarrierShippingLabel.tenant_id == tenant_id,
                CarrierShippingLabel.order_id == order.id,
            )
        )
        or 0
    )
    currency = (req.currency or order.currency or "USD").upper()

    adapter = build_adapter(carrier_code, http_client=http_client)
    booking = await adapter.book(
        CarrierBookingRequest(
            tenant_id=tenant_id,
            order_id=order.id,
            order_no=order.order_no,
            carrier_code=carrier_code,
            service_code=service_code,
            mode=req.mode,
            destination_country=country,
            destination_city=city,
            recipient_name=recipient,
            declared_weight_kg=weight_kg,
            currency=currency,
            lines=lines,
            sequence=sequence,
            credentials=credentials or {},
        )
    )

    tracking_provider = (
        TrackingProvider.TRACK17
        if req.mode == "live"
        else TrackingProvider.MANUAL
    )
    shipment = Shipment(
        tenant_id=tenant_id,
        order_id=order.id,
        tracking_provider=tracking_provider,
        tracking_number=booking.tracking_number,
        carrier_code=carrier_code,
        shipment_mode="parcel",
        standardized_status="info_received",
        actual_shipping_cost_base=booking.shipping_cost_estimate,
    )
    session.add(shipment)
    await session.flush()

    label = CarrierShippingLabel(
        tenant_id=tenant_id,
        order_id=order.id,
        shipment_id=shipment.id,
        carrier_code=carrier_code,
        service_code=service_code,
        tracking_number=booking.tracking_number,
        waybill_number=booking.waybill_number,
        label_format="pdf_4x6",
        mode=req.mode,
        shipping_cost_estimate=booking.shipping_cost_estimate,
        currency=booking.currency,
        status="created",
    )
    session.add(label)
    await session.flush()

    label.label_r2_key = await _persist_label_pdf(
        storage, label_storage_key(tenant_id, order.id, label.id), booking.pdf_bytes
    )

    tracking_registered = False
    if req.mode == "live" and os.getenv("TRACK17_API_KEY", "").strip():
        adapter_17track = tracking_adapter or Tracking17TrackAdapter()
        try:
            await adapter_17track.register_tracking(
                booking.tracking_number, booking.waybill_number
            )
            tracking_registered = True
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            # The waybill is already bought: registration is best-effort, never fatal.
            logger.warning("17track registration deferred: %s", type(exc).__name__)

    await record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_type="human_user",
        actor_id=str(principal.user_id),
        action="order.shipping_label_booked",
        resource_type="order",
        resource_id=order.id,
        before_state=(
            {
                "routing_evaluation_id": str(evaluation.id),
                "is_held": True,
                "hold_reason": evaluation.hold_reason,
                "override_hold": True,
                "override_reason": req.override_reason,
            }
            if evaluation is not None and evaluation.is_held
            else None
        ),
        after_state={
            "label_id": str(label.id),
            "shipment_id": str(shipment.id),
            "carrier_code": carrier_code,
            "service_code": service_code,
            "tracking_number": booking.tracking_number,
            "waybill_number": booking.waybill_number,
            "mode": req.mode,
            "shipping_cost_estimate": str(booking.shipping_cost_estimate),
            "currency": booking.currency,
            "label_stored": label.label_r2_key is not None,
            "tracking_registered": tracking_registered,
        },
    )

    return CarrierShippingLabelRead.model_validate(label), booking.pdf_bytes


async def get_label_pdf_bytes(
    session: AsyncSession,
    storage: ObjectStorageService | None,
    tenant_id: UUID,
    order_id: UUID,
    label_id: UUID,
) -> bytes:
    """Load the stored label PDF, or deterministically re-render it from the row."""
    label = await session.scalar(
        select(CarrierShippingLabel).where(
            CarrierShippingLabel.tenant_id == tenant_id,
            CarrierShippingLabel.order_id == order_id,
            CarrierShippingLabel.id == label_id,
        )
    )
    if label is None:
        raise AppError(
            slug="shipping-label-not-found",
            status_code=404,
            code="ERR_SHIPPING_LABEL_NOT_FOUND",
            title="Shipping Label Not Found",
            detail=f"Shipping label '{label_id}' not found for this order.",
        )
    if label.label_r2_key and storage is not None:
        try:
            return await storage.download_bytes(label.label_r2_key)
        except Exception as exc:  # noqa: BLE001 - fall through to the deterministic render
            logger.warning("label PDF fetch failed (%s); re-rendering", type(exc).__name__)

    order = (
        await session.execute(
            select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id)
        )
    ).scalar_one_or_none()
    if order is None:  # pragma: no cover - FK guarantees the order row exists
        raise AppError(
            slug="order-not-found",
            status_code=404,
            code="ERR_ORDER_NOT_FOUND",
            title="Order Not Found",
            detail=f"Order '{order_id}' not found.",
        )
    country, city, recipient = await _destination(session, tenant_id, order)
    lines = await _order_lines(session, tenant_id, order.id)
    weight = await _order_weight_kg(session, tenant_id, order.id)
    ctx = ThermalLabelContext(
        carrier_code=label.carrier_code,
        service_code=label.service_code,
        order_no=order.order_no,
        tracking_number=label.tracking_number,
        waybill_number=label.waybill_number,
        destination_country=country,
        destination_city=city,
        recipient_name=recipient,
        weight_kg=weight,
        currency=label.currency,
        shipping_cost_estimate=label.shipping_cost_estimate,
        lines=lines,
        mode=label.mode,
    )
    return render_thermal_label_pdf(ctx)


def label_storage_key(tenant_id: UUID, order_id: UUID, label_id: UUID) -> str:
    """Canonical object-storage key for a booked label PDF."""
    return f"tenants/{tenant_id}/shipping-labels/{order_id}/{label_id}.pdf"

