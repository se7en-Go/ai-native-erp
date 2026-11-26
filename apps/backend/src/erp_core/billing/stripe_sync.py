from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.billing.models import UsageLedger
from erp_core.billing.schemas import TopupPaymentRead
from erp_core.billing.wallet_service import OVERAGE_CENTS_PER_CREDIT, CreditWalletService
from erp_core.shared.config import get_settings

_STRIPE_URL = "https://api.stripe.com/v1"
_PAYMENT_ID = re.compile(r"pi_[a-zA-Z0-9_]{1,150}")
_INVOICE_ID = re.compile(r"in_[a-zA-Z0-9_]{1,150}")
_SUBSCRIPTION_ID = re.compile(r"sub_[a-zA-Z0-9_]{1,150}")
_TOPUP_FLAG = "erp_credit_topup"

#: Stripe event types that fund a wallet (idempotent topup).
TOPUP_EVENT_TYPES: frozenset[str] = frozenset({"payment_intent.succeeded", "invoice.paid"})
#: Stripe event types that downgrade the tenant plan / suspend overage.
DOWNGRADE_EVENT_TYPES: frozenset[str] = frozenset(
    {"invoice.payment_failed", "customer.subscription.deleted"}
)

StripeEventKind = Literal["topup", "downgrade"]


@dataclass(frozen=True)
class StripeBillingEvent:
    """One normalized, tenant-scoped billing event from a signed Stripe webhook."""

    kind: StripeEventKind
    tenant_id: UUID
    reference: str
    credits: int = 0
    reason: str = ""


def verify_stripe_webhook_signature(raw_body: bytes, sig_header: str, secret: str) -> bool:
    if not secret or not sig_header or len(sig_header) > 2048:
        return False
    fields: dict[str, list[str]] = {}
    for part in sig_header.split(","):
        name, sep, value = part.strip().partition("=")
        if sep:
            fields.setdefault(name, []).append(value)
    stamps = fields.get("t", [])
    if len(stamps) != 1 or not re.fullmatch(r"[0-9]{1,12}", stamps[0]):
        return False
    if abs(int(time.time()) - int(stamps[0])) > 300:
        return False
    expected = hmac.new(
        secret.encode(), stamps[0].encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return any(
        re.fullmatch(r"[a-fA-F0-9]{64}", candidate)
        and hmac.compare_digest(expected, candidate.lower())
        for candidate in fields.get("v1", [])
    )


class StripeMeterClientProtocol(Protocol):
    async def post_meter_event(
        self,
        *,
        identifier: str,
        event_name: str,
        stripe_customer_id: str,
        value: str,
        timestamp: int,
    ) -> str:
        """Post an idempotent meter event and return its provider identifier."""


class HttpStripeMeterClient:
    def __init__(
        self, secret_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.secret_key = secret_key if secret_key is not None else get_settings().STRIPE_SECRET_KEY
        self.transport = transport

    async def _post(self, path: str, data: dict[str, str], key: str) -> dict[str, Any]:
        if not self.secret_key:
            raise RuntimeError("Stripe is not configured.")
        try:
            async with httpx.AsyncClient(
                timeout=12, transport=self.transport, trust_env=False
            ) as client:
                response = await client.post(
                    _STRIPE_URL + path,
                    data=data,
                    headers={
                        "Authorization": f"Bearer {self.secret_key}",
                        "Idempotency-Key": key,
                    },
                )
                response.raise_for_status()
                answer = response.json()
                if isinstance(answer, dict):
                    return answer
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Stripe rejected the request or was unavailable.") from None
        raise RuntimeError("Invalid Stripe response.")

    async def post_meter_event(
        self,
        *,
        identifier: str,
        event_name: str,
        stripe_customer_id: str,
        value: str,
        timestamp: int,
    ) -> str:
        result = await self._post(
            "/billing/meter_events",
            {
                "identifier": identifier,
                "event_name": event_name,
                "timestamp": str(timestamp),
                "payload[stripe_customer_id]": stripe_customer_id,
                "payload[value]": value,
            },
            identifier,
        )
        if result.get("identifier") != identifier:
            raise RuntimeError("Invalid Stripe meter event response.")
        return identifier

    async def create_topup(self, tenant_id: UUID, credits: int, reference: str) -> TopupPaymentRead:
        amount = int(Decimal(credits) * OVERAGE_CENTS_PER_CREDIT)
        request_key = hashlib.sha256(f"{tenant_id}:{reference}".encode()).hexdigest()
        result = await self._post(
            "/payment_intents",
            {
                "amount": str(amount),
                "currency": "usd",
                "automatic_payment_methods[enabled]": "true",
                "metadata[tenant_id]": str(tenant_id),
                "metadata[erp_credit_topup]": "1",
                "metadata[credits]": str(credits),
            },
            f"erp-topup:{request_key}",
        )
        payment = result.get("id")
        secret = result.get("client_secret")
        if (
            not isinstance(payment, str)
            or not _PAYMENT_ID.fullmatch(payment)
            or not isinstance(secret, str)
        ):
            raise RuntimeError("Invalid Stripe PaymentIntent response.")
        return TopupPaymentRead(
            payment_intent_id=payment, client_secret=secret, amount_cents=amount
        )


def stripe_topup_details(event_payload: dict[str, Any]) -> tuple[UUID, str, int] | None:
    """Backwards-compatible accessor for the funding fields of a topup event."""
    event = interpret_stripe_event(event_payload)
    if event is None or event.kind != "topup":
        return None
    return event.tenant_id, event.reference, event.credits


def _stripe_object(event_payload: dict[str, Any]) -> dict[str, Any] | None:
    data = event_payload.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else None


def _tenant_id(metadata: dict[str, Any]) -> UUID | None:
    """Resolve the tenant from webhook metadata; `None` when Stripe has no mapping."""
    raw = metadata.get("tenant_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return UUID(raw.strip())
    except ValueError:
        raise ValueError("Invalid tenant id in Stripe metadata.") from None


def _topup_event(
    event_type: str, obj: dict[str, Any], metadata: dict[str, Any], identifier: re.Pattern[str]
) -> StripeBillingEvent | None:
    if metadata.get(_TOPUP_FLAG) != "1":
        return None
    reference = obj.get("id")
    if not isinstance(reference, str) or not identifier.fullmatch(reference):
        raise ValueError("Invalid paid object ID.")
    raw_credits = metadata.get("credits")
    if not isinstance(raw_credits, str) or not re.fullmatch(r"[0-9]{1,7}", raw_credits):
        raise ValueError("Invalid credit quantity.")
    credits = int(raw_credits)
    amount = (
        obj.get("amount_received")
        if event_type == "payment_intent.succeeded"
        else obj.get("amount_paid")
    )
    settled_status = obj.get("status")
    expected_status = "succeeded" if event_type == "payment_intent.succeeded" else "paid"
    if (
        not 0 < credits <= 1_000_000
        or type(amount) is not int
        or amount != int(Decimal(credits) * OVERAGE_CENTS_PER_CREDIT)
        or settled_status != expected_status
        or obj.get("currency") != "usd"
    ):
        raise ValueError("Paid amount or currency doesn't match the credit purchase.")
    tenant = _tenant_id(metadata)
    if tenant is None:
        return None
    return StripeBillingEvent(
        kind="topup", tenant_id=tenant, reference=reference, credits=credits
    )


def interpret_stripe_event(event_payload: dict[str, Any]) -> StripeBillingEvent | None:
    """Normalize a Stripe event into the closed-loop actions this system performs.

    Returns `None` for events that are not ours (no tenant metadata) so the
    webhook answers `200` and Stripe stops retrying. Raises `ValueError` when an
    event claims to be ours but its amounts do not add up (answered as `422`).
    """
    event_type = event_payload.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type not in TOPUP_EVENT_TYPES and event_type not in DOWNGRADE_EVENT_TYPES:
        return None
    obj = _stripe_object(event_payload)
    metadata = obj.get("metadata") if isinstance(obj, dict) else None
    if not isinstance(obj, dict) or not isinstance(metadata, dict):
        return None

    if event_type == "payment_intent.succeeded":
        return _topup_event(event_type, obj, metadata, _PAYMENT_ID)
    if event_type == "invoice.paid":
        return _topup_event(event_type, obj, metadata, _INVOICE_ID)

    reference = obj.get("id")
    identifier = _INVOICE_ID if event_type == "invoice.payment_failed" else _SUBSCRIPTION_ID
    reason = (
        "invoice_payment_failed"
        if event_type == "invoice.payment_failed"
        else "subscription_deleted"
    )
    if not isinstance(reference, str) or not identifier.fullmatch(reference):
        raise ValueError("Invalid billing object ID.")
    tenant = _tenant_id(metadata)
    if tenant is None:
        return None
    return StripeBillingEvent(
        kind="downgrade", tenant_id=tenant, reference=reference, reason=reason
    )


class StripeMeterSyncService:
    def __init__(self, stripe_client: StripeMeterClientProtocol | None = None) -> None:
        self.stripe_client = stripe_client or HttpStripeMeterClient()

    async def sync_unsynced_events(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        stripe_customer_id: str,
        batch_size: int = 100,
    ) -> int:
        if not 1 <= batch_size <= 100 or not stripe_customer_id:
            raise ValueError("Invalid Stripe meter batch configuration.")
        rows = (
            await session.scalars(
                select(UsageLedger)
                .where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.stripe_meter_synced.is_(False),
                )
                .order_by(UsageLedger.created_at, UsageLedger.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            identifier = hashlib.sha256(f"{tenant_id}:{row.idempotency_key}".encode()).hexdigest()
            # Stripe sums integers. Meter values use the ledger's 1/10,000-unit resolution.
            value = str(int(row.units_consumed * Decimal("10000")))
            reference = await self.stripe_client.post_meter_event(
                identifier=identifier,
                event_name=f"erp_{row.metric_type}",
                stripe_customer_id=stripe_customer_id,
                value=value,
                timestamp=int(row.created_at.timestamp()),
            )
            await session.execute(
                update(UsageLedger)
                .where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.id == row.id,
                )
                .values(stripe_meter_synced=True, stripe_meter_event_id=reference)
            )
        return len(rows)

    async def handle_billing_event(
        self,
        session: AsyncSession,
        *,
        event_payload: dict[str, Any],
    ) -> dict[str, str]:
        """Apply one signed Stripe event exactly once (replays are no-ops)."""
        event = interpret_stripe_event(event_payload)
        if event is None:
            return {"status": "ignored"}
        wallets = CreditWalletService()
        if event.kind == "topup":
            applied = await wallets.apply_stripe_topup(
                session, event.tenant_id, event.reference, event.credits
            )
            return {"status": "credited" if applied else "already_credited"}
        downgraded = await wallets.apply_billing_downgrade(
            session, event.tenant_id, event.reference, reason=event.reason
        )
        return {"status": "downgraded" if downgraded else "already_downgraded"}
