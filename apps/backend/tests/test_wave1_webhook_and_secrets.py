"""Wave 1 P0/P1 security regressions: webhook tenant isolation and secret stripping."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from erp_core.channels.adapters.base import (
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
)
from erp_core.channels.ingress_worker import filter_webhook_batch_for_row
from erp_core.channels.webhook_router import sanitize_webhook_headers
from erp_core.shared.enums import DeliveryStatus, IdentityType
from erp_core.shared.errors import register_exception_handlers

TENANT_A_ACCOUNT = "109900000000001"
TENANT_B_ACCOUNT = "209900000000002"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def inbound_event(
    *,
    account: str,
    external_event_id: str,
    external_message_id: str | None,
    body: str = "hello",
) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        external_event_id=external_event_id,
        external_account_id=account,
        external_message_id=external_message_id,
        external_thread_id=f"thread-{external_message_id or external_event_id}",
        sender_identity_type=IdentityType.WHATSAPP_WAID,
        sender_identity_value="15551234567",
        sender_display_name="Buyer",
        body_text=body,
        source_timestamp=T0,
    )


def status_event(
    *,
    account: str,
    external_message_id: str,
    source_external_event_id: str | None = None,
) -> NormalizedStatusEvent:
    return NormalizedStatusEvent(
        external_message_id=external_message_id,
        external_account_id=account,
        delivery_status=DeliveryStatus.DELIVERED,
        timestamp=T0,
        source_external_event_id=source_external_event_id,
    )


def meta_style_batch() -> NormalizedWebhookBatch:
    """One WhatsApp envelope carrying events for two different tenant accounts."""
    return NormalizedWebhookBatch(
        inbound_events=[
            inbound_event(
                account=TENANT_A_ACCOUNT, external_event_id="mid-a1", external_message_id="mid-a1"
            ),
            inbound_event(
                account=TENANT_B_ACCOUNT, external_event_id="mid-b1", external_message_id="mid-b1"
            ),
            inbound_event(
                account=TENANT_A_ACCOUNT, external_event_id="mid-a2", external_message_id="mid-a2"
            ),
        ],
        status_events=[
            status_event(account=TENANT_A_ACCOUNT, external_message_id="mid-a-out"),
            status_event(account=TENANT_B_ACCOUNT, external_message_id="mid-b-out"),
        ],
        account_updates=[{"account": TENANT_A_ACCOUNT, "event": "account_update"}],
    )


def test_filter_drops_cross_account_inbound_and_status_events() -> None:
    batch = meta_style_batch()

    filtered = filter_webhook_batch_for_row(
        batch,
        expected_account_id=TENANT_A_ACCOUNT,
        expected_event_id="mid-a1",
    )

    # Only Tenant A's events for this row survive; Tenant B's payload never reaches
    # Tenant A's tenant session.
    assert [event.external_message_id for event in filtered.inbound_events] == ["mid-a1"]
    assert [event.external_account_id for event in filtered.inbound_events] == [TENANT_A_ACCOUNT]
    # No status event matches "mid-a1", so account filtering alone applies.
    assert [event.external_message_id for event in filtered.status_events] == ["mid-a-out"]
    assert filtered.account_updates == [{"account": TENANT_A_ACCOUNT, "event": "account_update"}]

    # The helper is pure: the parsed batch handed in is never mutated.
    assert len(batch.inbound_events) == 3
    assert len(batch.status_events) == 2


def test_filter_keeps_events_when_row_event_id_is_synthetic() -> None:
    batch = meta_style_batch()
    synthetic_svix_id = "msg_2k9Qsvix0pQ1Lm3zSyntheticRowKey"

    filtered = filter_webhook_batch_for_row(
        batch,
        expected_account_id=TENANT_A_ACCOUNT,
        expected_event_id=synthetic_svix_id,
    )

    # A synthetic/composite row key matches no external_message_id, so it must never
    # drop the legitimate events of the target account.
    assert [event.external_message_id for event in filtered.inbound_events] == ["mid-a1", "mid-a2"]
    assert [event.external_message_id for event in filtered.status_events] == ["mid-a-out"]

    sha_like = "b5c0f1e2d3a495867120a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8"
    filtered_sha = filter_webhook_batch_for_row(
        batch,
        expected_account_id=TENANT_A_ACCOUNT,
        expected_event_id=sha_like,
    )
    assert len(filtered_sha.inbound_events) == 2
    assert len(filtered_sha.status_events) == 1


def test_filter_narrows_status_events_by_source_external_event_id() -> None:
    batch = NormalizedWebhookBatch(
        inbound_events=[],
        status_events=[
            status_event(
                account=TENANT_A_ACCOUNT,
                external_message_id="mid-a-out-1",
                source_external_event_id="status:row-1",
            ),
            status_event(
                account=TENANT_A_ACCOUNT,
                external_message_id="mid-a-out-2",
                source_external_event_id="status:row-2",
            ),
            status_event(
                account=TENANT_B_ACCOUNT,
                external_message_id="mid-b-out-1",
                source_external_event_id="status:row-1",
            ),
        ],
    )

    filtered = filter_webhook_batch_for_row(
        batch,
        expected_account_id=TENANT_A_ACCOUNT,
        expected_event_id="status:row-1",
    )

    assert [event.external_message_id for event in filtered.status_events] == ["mid-a-out-1"]


def test_filter_keeps_events_with_empty_account_and_no_expected_account() -> None:
    batch = NormalizedWebhookBatch(
        inbound_events=[
            inbound_event(
                account="", external_event_id="mid-empty-acct", external_message_id="mid-empty-acct"
            )
        ],
        status_events=[status_event(account="", external_message_id="mid-empty-acct")],
    )

    filtered = filter_webhook_batch_for_row(
        batch,
        expected_account_id=None,
        expected_event_id=None,
    )

    # Unknown account on either side is never treated as proof of a cross-tenant event.
    assert len(filtered.inbound_events) == 1
    assert len(filtered.status_events) == 1


def test_sanitize_webhook_headers_strips_auth_material() -> None:
    headers = {
        "X-Telegram-Bot-Api-Secret-Token": "telegram-secret-token",
        "Authorization": "Bearer super-secret-access-token",
        "Proxy-Authorization": "Basic dXNlcjpwYXNz",
        "Cookie": "session=abc123",
        "Set-Cookie": "session=abc123",
        "X-Api-Key": "sk-live-should-not-persist",
        "X-Webhook-Secret": "webhook-shared-secret",
        "X-Secret-Token": "another-secret",
        "content-type": "application/json",
        "svix-id": "msg_2k9Qsvix0pQ1Lm3z",
        "x-hub-signature-256": "sha256=deadbeef",
    }

    sanitized = sanitize_webhook_headers(headers)

    assert sanitized == {
        "content-type": "application/json",
        "svix-id": "msg_2k9Qsvix0pQ1Lm3z",
        "x-hub-signature-256": "sha256=deadbeef",
    }
    # Signature verification needs the originals, so sanitizing must not mutate them.
    assert headers["Authorization"] == "Bearer super-secret-access-token"
    assert json.dumps(sanitize_webhook_headers(headers)).find("super-secret-access-token") == -1


class SecretTokensBody(BaseModel):
    secret: str = Field(min_length=64, description="bot token")
    port: int


def test_validation_error_never_echoes_submitted_secret() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/api/v1/channels/credentials")
    async def create_credential(body: SecretTokensBody) -> dict[str, str]:
        return {"secret": body.secret}

    client = TestClient(app)
    response = client.post(
        "/api/v1/channels/credentials",
        json={"secret": "SUPER_SECRET_BOT_TOKEN_12345", "port": "not-a-port"},
    )

    assert response.status_code == 422
    assert "SUPER_SECRET_BOT_TOKEN_12345" not in response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == "ERR_VALIDATION"
    assert body["status"] == 422
    details = json.loads(body["detail"])
    assert details, "validation summary must list the failing fields"
    assert {item["loc"][-1] for item in details} == {"secret", "port"}
    for item in details:
        assert set(item) == {"loc", "type", "msg"}
