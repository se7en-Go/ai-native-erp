"""Contract tests for the twelve marketplace buyer-IM adapters.

Every assertion here is a production invariant:

* a genuine buyer chat is recognized and an order/fulfillment push never is;
* ``extract_external_event_ids`` and ``parse_webhook`` agree on the exact
  ``(external_event_id, external_account_id)`` pair (``filter_webhook_batch_for_row``
  compares those strings, so any drift silently drops the event);
* webhook verification fails closed and inbound messages normalize onto one buyer
  identity type with a non-empty thread;
* outbound sends are really signed, retryable only when retrying can help, and never
  leak credentials into ``error_message``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.adapters.marketplace_im import (
    IM_EVENT_HEADER,
    MARKETPLACE_IM_CHANNELS,
    MARKETPLACE_IM_SLUGS,
    STOP_KEYWORDS,
    TOPIC_HEADER_BY_SLUG,
    build_marketplace_im_adapters,
    is_marketplace_im_event,
    is_stop_command,
)
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    IdentityType,
    MessageContentType,
)

SESSION = cast(AsyncSession, None)
TENANT_ID = uuid4()
CHANNEL_ACCOUNT_ID = uuid4()
BUYER_TEXT = "Where is my parcel?"
OUTBOUND_TEXT = "Hello buyer"
TIKTOK_APP_KEY = "tt-app-key-1"


# ---------------------------------------------------------------------------------
# per-platform fixtures
# ---------------------------------------------------------------------------------


def _hex_signature(header: str) -> Callable[[bytes, str], dict[str, str]]:
    def build(body: bytes, secret: str) -> dict[str, str]:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {header: digest}

    return build


def _tiktok_signature(body: bytes, secret: str) -> dict[str, str]:
    digest = hmac.new(
        secret.encode(), TIKTOK_APP_KEY.encode() + body, hashlib.sha256
    ).hexdigest()
    return {"authorization": digest, "x-tts-app-key": TIKTOK_APP_KEY}


@dataclass(frozen=True)
class Platform:
    slug: str
    payload: dict[str, Any]
    headers: dict[str, str]
    signature: Callable[[bytes, str], dict[str, str]]
    secret: str
    body_path: tuple[str, ...]
    expected_account: str
    expected_buyer: str
    expected_event_id: str
    expected_thread: str
    conversation_mode: ConversationMode
    credentials: dict[str, str]
    send_metadata: dict[str, str]
    send_url_suffix: str
    send_params: tuple[str, ...] = ()
    send_headers: tuple[str, ...] = ()
    extra_identities: tuple[tuple[IdentityType, str], ...] = field(default_factory=tuple)

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType(self.slug)

    @property
    def raw_body(self) -> bytes:
        return json.dumps(self.payload, separators=(",", ":"), sort_keys=True).encode()

    def signed_headers(self) -> dict[str, str]:
        return {**self.headers, **self.signature(self.raw_body, self.secret)}


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        slug="shopee",
        payload={
            "shop_id": "778899",
            "from_id": "sg-buyer-1",
            "to_id": "778899",
            "conversation_id": "sg-conv-1",
            "message_id": "sg-msg-1",
            "message_type": "text",
            "content": {"text": BUYER_TEXT},
            "created_timestamp": 1730000000,
        },
        headers={"x-shopee-shop-id": "778899", "x-shopee-topic": "buyer_chat_message"},
        signature=_hex_signature("x-shopee-hmac-sha256"),
        secret="shopee-partner-key",
        body_path=("content", "text"),
        expected_account="778899",
        expected_buyer="sg-buyer-1",
        expected_event_id="sg-msg-1",
        expected_thread="sg-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "partner_id": "10001",
            "partner_key": "shopee-partner-key",
            "access_token": "shopee-access-token",
            "shop_id": "778899",
        },
        send_metadata={"conversation_id": "sg-conv-1"},
        send_url_suffix="/api/v2/sellerchat/send_message",
        send_params=("partner_id", "timestamp", "sign", "access_token", "shop_id"),
    ),
    Platform(
        slug="lazada",
        payload={
            "seller_id": "lz-seller-1",
            "buyer_id": "lz-buyer-1",
            "session_id": "lz-sess-1",
            "message_id": "lz-msg-1",
            "content": BUYER_TEXT,
            "type": "text",
        },
        headers={"x-lazada-seller-id": "lz-seller-1", "x-lazada-topic": "im_message"},
        signature=_hex_signature("x-lazada-signature"),
        secret="lazada-app-secret",
        body_path=("content",),
        expected_account="lz-seller-1",
        expected_buyer="lz-buyer-1",
        expected_event_id="lz-msg-1",
        expected_thread="lz-sess-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "app_key": "lazada-app-key",
            "app_secret": "lazada-app-secret",
            "access_token": "lazada-access-token",
        },
        send_metadata={"conversation_id": "lz-sess-1"},
        send_url_suffix="/rest/im/message/send",
        send_params=("app_key", "sign", "sign_method"),
    ),
    Platform(
        slug="temu",
        payload={
            "mall_id": "tm-mall-1",
            "buyer_id": "tm-buyer-1",
            "session_id": "tm-sess-1",
            "msg_id": "tm-msg-1",
            "message_type": "text",
            "content": {"text": BUYER_TEXT},
        },
        headers={"x-temu-mall-id": "tm-mall-1", "x-temu-topic": "buyer_message"},
        signature=_hex_signature("x-temu-signature"),
        secret="temu-app-secret",
        body_path=("content", "text"),
        expected_account="tm-mall-1",
        expected_buyer="tm-buyer-1",
        expected_event_id="tm-msg-1",
        expected_thread="tm-sess-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "app_key": "temu-app-key",
            "app_secret": "temu-app-secret",
            "access_token": "temu-access-token",
            "mall_id": "tm-mall-1",
        },
        send_metadata={"conversation_id": "tm-sess-1"},
        send_url_suffix="/openapi/router",
        send_params=("type", "app_key", "sign"),
    ),
    Platform(
        slug="shein",
        payload={
            "seller_id": "sh-seller-1",
            "buyer_id": "sh-buyer-1",
            "conversation_id": "sh-conv-1",
            "message_id": "sh-msg-1",
            "sender_type": "USER",
            "content": BUYER_TEXT,
        },
        headers={"x-shein-seller-id": "sh-seller-1", "x-shein-topic": "chat_message"},
        signature=_hex_signature("x-shein-signature"),
        secret="shein-secret-key",
        body_path=("content",),
        expected_account="sh-seller-1",
        expected_buyer="sh-buyer-1",
        expected_event_id="sh-msg-1",
        expected_thread="sh-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "open_key_id": "shein-open-key",
            "secret_key": "shein-secret-key",
            "seller_id": "sh-seller-1",
        },
        send_metadata={"conversation_id": "sh-conv-1"},
        send_url_suffix="/open-api/im/message/send",
        send_headers=("x-shein-open-key-id", "x-shein-signature"),
    ),
    Platform(
        slug="aliexpress",
        payload={
            "seller_id": "ae-seller-1",
            "buyer_id": "ae-buyer-1",
            "conversation_id": "ae-conv-1",
            "message_id": "ae-msg-1",
            "content": BUYER_TEXT,
        },
        headers={
            "x-aliexpress-seller-id": "ae-seller-1",
            "x-aliexpress-topic": "im_message_received",
        },
        signature=_hex_signature("x-aliexpress-hmac-sha256"),
        secret="aliexpress-app-secret",
        body_path=("content",),
        expected_account="ae-seller-1",
        expected_buyer="ae-buyer-1",
        expected_event_id="ae-msg-1",
        expected_thread="ae-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "app_key": "aliexpress-app-key",
            "app_secret": "aliexpress-app-secret",
            "access_token": "aliexpress-access-token",
        },
        send_metadata={"conversation_id": "ae-conv-1"},
        send_url_suffix="/sync",
        send_params=("method", "app_key", "sign"),
    ),
    Platform(
        slug="ozon",
        payload={
            "client_id": "oz-client-1",
            "chat_id": "oz-chat-1",
            "message_id": "oz-msg-1",
            "seller_id": "oz-seller-1",
            "sender": {"type": "customer", "id": "oz-buyer-1", "name": "Ivan"},
            "content": {"text": BUYER_TEXT},
        },
        headers={"x-ozon-client-id": "oz-client-1", "x-ozon-topic": "chat_message"},
        signature=_hex_signature("x-ozon-signature"),
        secret="ozon-api-key",
        body_path=("content", "text"),
        expected_account="oz-client-1",
        expected_buyer="oz-buyer-1",
        expected_event_id="oz-msg-1",
        expected_thread="oz-chat-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={"client_id": "oz-client-1", "api_key": "ozon-api-key"},
        send_metadata={"chat_id": "oz-chat-1"},
        send_url_suffix="/v1/chat/send/message",
        send_headers=("client-id", "api-key"),
    ),
    Platform(
        slug="walmart",
        payload={
            "seller_id": "wm-seller-1",
            "conversationId": "wm-conv-1",
            "messageId": "wm-msg-1",
            "senderId": "wm-buyer-1",
            "receiverId": "wm-seller-1",
            "messageText": BUYER_TEXT,
            "messageType": "text",
            "createdAt": "2026-11-24T10:00:00Z",
        },
        headers={"x-walmart-seller-id": "wm-seller-1", "x-walmart-topic": "messages"},
        signature=_hex_signature("wm-sec.signature"),
        secret="walmart-client-secret",
        body_path=("messageText",),
        expected_account="wm-seller-1",
        expected_buyer="wm-buyer-1",
        expected_event_id="wm-msg-1",
        expected_thread="wm-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={"access_token": "walmart-access-token", "seller_id": "wm-seller-1"},
        send_metadata={"conversation_id": "wm-conv-1"},
        send_url_suffix="/v3/messages/send",
        send_headers=("wm_sec.access_token",),
    ),
    Platform(
        slug="mercado_libre",
        payload={
            "user_id": "ml-seller-1",
            "topic": "messages",
            "conversation_id": "ml-conv-1",
            "message": {
                "id": "ml-msg-1",
                "text": BUYER_TEXT,
                "from": {"user_id": "ml-buyer-1", "name": "Ana"},
            },
        },
        headers={"x-meli-seller-id": "ml-seller-1", "x-meli-topic": "messages"},
        signature=_hex_signature("x-signature"),
        secret="meli-webhook-secret",
        body_path=("message", "text"),
        expected_account="ml-seller-1",
        expected_buyer="ml-buyer-1",
        expected_event_id="ml-msg-1",
        expected_thread="ml-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={"access_token": "meli-access-token", "seller_id": "ml-seller-1"},
        send_metadata={"pack_id": "pack-77"},
        send_url_suffix="/messages/packs/pack-77/sellers/ml-seller-1",
        send_headers=("authorization",),
    ),
    Platform(
        slug="tiktok_shop",
        payload={
            "shop_id": "tt-shop-1",
            "data": {
                "conversation_id": "tt-conv-1",
                "message_id": "tt-msg-1",
                "type": "TEXT",
                "content": {"text": BUYER_TEXT},
                "create_time": 1730000001,
                "buyer_user_id": "tt-buyer-1",
            },
        },
        headers={
            "x-tts-shop-domain": "tt-shop-1",
            "x-tts-topic": "customer_service_message",
        },
        signature=_tiktok_signature,
        secret="tts-app-secret",
        body_path=("data", "content", "text"),
        expected_account="tt-shop-1",
        expected_buyer="tt-buyer-1",
        expected_event_id="tt-msg-1",
        expected_thread="tt-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "app_key": TIKTOK_APP_KEY,
            "app_secret": "tts-app-secret",
            "access_token": "tts-access-token",
            "shop_cipher": "tts-shop-cipher",
        },
        send_metadata={"conversation_id": "tt-conv-1"},
        send_url_suffix="/customer_service/202309/conversations/tt-conv-1/messages",
        send_params=("app_key", "sign"),
        send_headers=("x-tts-access-token",),
    ),
    Platform(
        slug="ebay",
        payload={
            "seller_username": "eb-seller",
            "notification": {
                "data": {
                    "messageId": "eb-msg-1",
                    "senderId": "eb-buyer-1",
                    "conversationId": "eb-conv-1",
                    "messageText": BUYER_TEXT,
                    "createdAt": 1730000002,
                }
            },
        },
        headers={"x-ebay-seller-username": "eb-seller", "topic": "member_message"},
        signature=_hex_signature("x-ebay-signature"),
        secret="ebay-client-secret",
        body_path=("notification", "data", "messageText"),
        expected_account="eb-seller",
        expected_buyer="eb-buyer-1",
        expected_event_id="eb-msg-1",
        expected_thread="eb-conv-1",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={"access_token": "ebay-access-token"},
        send_metadata={"conversation_id": "eb-conv-1"},
        send_url_suffix="/commerce/notification/v1/member_message",
        send_headers=("authorization",),
    ),
    Platform(
        slug="amazon_sp",
        payload={
            "seller_id": "am-seller-1",
            "order_id": "111-2223334-5556667",
            "buyer_id": "am-buyer-1",
            "message_id": "am-msg-1",
            "message": {"text": BUYER_TEXT},
        },
        headers={"x-amazon-seller-id": "am-seller-1", "topic": "buyer_message"},
        signature=_hex_signature("x-amazon-signature"),
        secret="amazon-lwa-secret",
        body_path=("message", "text"),
        expected_account="am-seller-1",
        expected_buyer="am-buyer-1",
        expected_event_id="am-msg-1",
        expected_thread="111-2223334-5556667",
        conversation_mode=ConversationMode.B2C_SUPPORT,
        credentials={
            "access_token": "amazon-access-token",
            "marketplace_id": "ATVPDKIKX0DER",
        },
        send_metadata={"order_id": "111-2223334-5556667"},
        send_url_suffix=(
            "/messaging/v1/orders/111-2223334-5556667/messages/confirmCustomizationDetails"
        ),
        send_headers=("x-amz-access-token",),
    ),
    Platform(
        slug="alibaba_email",
        payload={
            "store_id": "al-store-1",
            "buyer_id": "al-buyer-1",
            "inquiry_id": "al-inq-1",
            "message_id": "al-msg-1",
            "subject": "RFQ 2000 pcs",
            "content": BUYER_TEXT,
            "buyer_email": "buyer@example.com",
            "country": "US",
        },
        headers={"x-alibaba-store-id": "al-store-1", "topic": "inquiry_message"},
        signature=_hex_signature("x-alibaba-signature"),
        secret="alibaba-app-secret",
        body_path=("content",),
        expected_account="al-store-1",
        expected_buyer="al-buyer-1",
        expected_event_id="al-msg-1",
        expected_thread="al-inq-1",
        conversation_mode=ConversationMode.B2B_INQUIRY,
        credentials={
            "app_key": "alibaba-app-key",
            "app_secret": "alibaba-app-secret",
            "access_token": "alibaba-access-token",
            "store_id": "al-store-1",
        },
        send_metadata={"conversation_id": "al-inq-1"},
        send_url_suffix="/alibaba/icbu/im/message/send",
        send_params=("app_key", "sign"),
        extra_identities=((IdentityType.EMAIL, "buyer@example.com"),),
    ),
)

SLUGS = tuple(platform.slug for platform in PLATFORMS)


def platform_case(slug: str) -> Platform:
    for platform in PLATFORMS:
        if platform.slug == slug:
            return platform
    raise AssertionError(f"unknown platform {slug}")


def adapters() -> dict[ChannelType, MessagingChannelAdapter]:
    return build_marketplace_im_adapters()


def adapter_for(platform: Platform) -> MessagingChannelAdapter:
    return adapters()[platform.channel_type]


def with_body(platform: Platform, text: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(platform.payload))
    cursor: dict[str, Any] = payload
    for key in platform.body_path[:-1]:
        cursor = cursor[key]
    cursor[platform.body_path[-1]] = text
    return payload


TOPIC_HEADER_NAMES = frozenset({*
    TOPIC_HEADER_BY_SLUG.values(),
    "topic",
    "x-erp-topic",
})
IDENTIFIER_KEYS = frozenset(
    {
        "id",
        "message_id",
        "messageId",
        "msg_id",
        "msgId",
        "event_id",
        "eventId",
        "notification_id",
        "notificationId",
        "tts_notification_id",
        "uuid",
    }
)


def with_topic(platform: Platform, topic: str) -> dict[str, str]:
    """Replace every topic header with ``topic`` (platform header included)."""
    headers = {
        name: value
        for name, value in platform.headers.items()
        if name.lower() not in TOPIC_HEADER_NAMES
    }
    return {**headers, "topic": topic}


def scrub_identifiers(value: Any) -> Any:
    """Drop every id-ish field at any depth, forcing the digest fallback."""
    if isinstance(value, dict):
        return {
            key: scrub_identifiers(inner)
            for key, inner in value.items()
            if key not in IDENTIFIER_KEYS
        }
    if isinstance(value, list):
        return [scrub_identifiers(item) for item in value]
    return value


def outbound_request(
    platform: Platform,
    *,
    recipient_id: str | None = None,
    body_text: str = OUTBOUND_TEXT,
) -> OutboundMessageRequest:
    return OutboundMessageRequest(
        tenant_id=TENANT_ID,
        channel_account_id=CHANNEL_ACCOUNT_ID,
        channel_type=platform.channel_type,
        external_account_id=platform.expected_account,
        recipient_id=recipient_id or f"{platform.slug}:buyer-42",
        conversation_id=uuid4(),
        message_id=uuid4(),
        content_type=MessageContentType.TEXT,
        body_text=body_text,
        metadata=dict(platform.send_metadata),
    )


async def parse(platform: Platform, payload: dict[str, Any]) -> NormalizedWebhookBatch:
    return await adapter_for(platform).parse_webhook(
        SESSION, TENANT_ID, CHANNEL_ACCOUNT_ID, payload, platform.headers
    )


# ---------------------------------------------------------------------------------
# channel fleet inventory
# ---------------------------------------------------------------------------------


def test_marketplace_im_channels_cover_the_whole_marketplace_fleet() -> None:
    expected = {
        ChannelType.SHOPEE,
        ChannelType.LAZADA,
        ChannelType.TEMU,
        ChannelType.SHEIN,
        ChannelType.ALIEXPRESS,
        ChannelType.OZON,
        ChannelType.WALMART,
        ChannelType.MERCADO_LIBRE,
        ChannelType.TIKTOK_SHOP,
        ChannelType.EBAY,
        ChannelType.AMAZON_SP,
        ChannelType.ALIBABA_EMAIL,
    }
    assert len(MARKETPLACE_IM_CHANNELS) == 12
    assert set(MARKETPLACE_IM_CHANNELS) == expected
    assert frozenset(channel.value for channel in expected) == MARKETPLACE_IM_SLUGS


def test_factory_builds_one_adapter_per_marketplace_channel() -> None:
    built = adapters()
    assert set(built) == set(MARKETPLACE_IM_CHANNELS)
    for channel_type, adapter in built.items():
        assert channel_type in adapter.channel_types
    # Distinct platforms must not share one generic adapter instance.
    assert len({id(adapter) for adapter in built.values()}) == 12


def test_factory_forwards_the_http_transport() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    built = build_marketplace_im_adapters(http_transport=transport)
    for adapter in built.values():
        assert adapter.http_transport is transport  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------
# buyer-IM event classification
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_is_marketplace_im_event_accepts_buyer_chat_topics(slug: str) -> None:
    platform = platform_case(slug)
    assert is_marketplace_im_event(slug, platform.payload, platform.headers) is True


@pytest.mark.parametrize("slug", SLUGS)
def test_is_marketplace_im_event_honours_the_internal_route_flag(slug: str) -> None:
    platform = platform_case(slug)
    headers = {**platform.headers, IM_EVENT_HEADER: "true"}
    assert is_marketplace_im_event(slug, platform.payload, headers) is True
    # A non-truthy flag neither forces nor fabricates an IM delivery.
    assert is_marketplace_im_event(slug, {}, {IM_EVENT_HEADER: "no"}) is False


@pytest.mark.parametrize("slug", SLUGS)
def test_is_marketplace_im_event_rejects_order_topics(slug: str) -> None:
    platform = platform_case(slug)
    for topic in ("order_status_update", "posting_status_changed", "orders/create"):
        assert is_marketplace_im_event(slug, platform.payload, with_topic(platform, topic)) is False


@pytest.mark.parametrize(
    ("slug", "topic_header"), sorted(TOPIC_HEADER_BY_SLUG.items())
)
def test_is_marketplace_im_event_reads_the_platform_topic_header(
    slug: str, topic_header: str
) -> None:
    platform = platform_case(slug)
    headers = {topic_header: "posting_status_changed"}
    assert is_marketplace_im_event(slug, platform.payload, headers) is False
    assert is_marketplace_im_event(slug, {}, {topic_header: "buyer_chat_message"}) is True


@pytest.mark.parametrize("slug", SLUGS)
def test_is_marketplace_im_event_checks_payload_event_type(slug: str) -> None:
    platform = platform_case(slug)
    im_payload = {**platform.payload, "event_type": "chat.message.created"}
    assert is_marketplace_im_event(slug, im_payload, {}) is True
    order_payload = {**platform.payload, "event_type": "order_status_update"}
    assert is_marketplace_im_event(slug, order_payload, {}) is False


def test_is_marketplace_im_event_detects_shopee_push_code_ten() -> None:
    payload = {"shop_id": "1", "push_code": 10, "data": {"content": {"text": "hi"}}}
    assert is_marketplace_im_event("shopee", payload, {}) is True
    assert is_marketplace_im_event("shopee", {**payload, "push_code": 3}, {}) is False


def test_is_marketplace_im_event_uses_chat_shape_without_a_topic() -> None:
    payload = {
        "conversation_id": "conv-1",
        "content": {"text": "still there?"},
    }
    assert is_marketplace_im_event("shopee", payload, {}) is True
    # A thread id without any message content is not a buyer message.
    assert is_marketplace_im_event("shopee", {"conversation_id": "conv-1"}, {}) is False


@pytest.mark.parametrize(
    "channel_type",
    ["woocommerce", "shopify", "telegram", "", "made_in_china_email"],
)
def test_is_marketplace_im_event_ignores_foreign_channels(channel_type: str) -> None:
    platform = platform_case("shopee")
    assert is_marketplace_im_event(channel_type, platform.payload, platform.headers) is False


def test_is_marketplace_im_event_normalizes_channel_slug() -> None:
    platform = platform_case("shopee")
    assert is_marketplace_im_event("SHOPEE", platform.payload, platform.headers) is True
    assert (
        is_marketplace_im_event("ChannelType.SHOPEE", platform.payload, platform.headers) is True
    )


# ---------------------------------------------------------------------------------
# inbound signature verification
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_verify_webhook_signature_accepts_the_platform_digest(slug: str) -> None:
    platform = platform_case(slug)
    raw_body = platform.raw_body
    headers = platform.signed_headers()
    assert adapter_for(platform).verify_webhook_signature(raw_body, headers, platform.secret)


@pytest.mark.parametrize("slug", SLUGS)
def test_verify_webhook_signature_fails_closed(slug: str) -> None:
    platform = platform_case(slug)
    raw_body = platform.raw_body
    adapter = adapter_for(platform)
    signed = platform.signed_headers()

    # Empty secret is never trusted.
    assert adapter.verify_webhook_signature(raw_body, signed, "") is False
    # No signature header at all.
    assert adapter.verify_webhook_signature(raw_body, platform.headers, platform.secret) is False
    # Tampered body keeps the old digest.
    assert (
        adapter.verify_webhook_signature(raw_body + b" ", signed, platform.secret) is False
    )


def test_tiktok_accepts_a_raw_body_hmac_without_the_app_key_header() -> None:
    platform = platform_case("tiktok_shop")
    raw_body = platform.raw_body
    digest = hmac.new(platform.secret.encode(), raw_body, hashlib.sha256).hexdigest()
    assert adapter_for(platform).verify_webhook_signature(
        raw_body, {"authorization": digest}, platform.secret
    )
    assert not adapter_for(platform).verify_webhook_signature(
        raw_body, {"authorization": "deadbeef"}, platform.secret
    )


def test_shopee_accepts_a_callback_url_signed_body() -> None:
    platform = platform_case("shopee")
    raw_body = platform.raw_body
    callback_url = "https://erp.example.com/api/v1/channels/shopee/webhook"
    digest = hmac.new(
        platform.secret.encode(), callback_url.encode() + b"|" + raw_body, hashlib.sha256
    ).hexdigest()
    headers = {"x-shopee-hmac-sha256": digest, "x-erp-callback-url": callback_url}
    assert adapter_for(platform).verify_webhook_signature(raw_body, headers, platform.secret)


# ---------------------------------------------------------------------------------
# row identity contract: extract_external_event_ids == parse_webhook
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
async def test_extract_external_event_ids_matches_parse_webhook(slug: str) -> None:
    platform = platform_case(slug)
    adapter = adapter_for(platform)
    extracted = adapter.extract_external_event_ids(
        platform.payload, platform.raw_body, platform.headers
    )
    assert extracted == [(platform.expected_event_id, platform.expected_account)]

    batch = await parse(platform, platform.payload)
    assert len(batch.inbound_events) == 1
    event = batch.inbound_events[0]
    assert (event.external_event_id, event.external_account_id) == extracted[0]


@pytest.mark.parametrize("slug", SLUGS)
async def test_extract_falls_back_to_the_body_digest_without_ids(slug: str) -> None:
    platform = platform_case(slug)
    payload = scrub_identifiers(platform.payload)
    raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    expected_digest = hashlib.sha256(raw_body).hexdigest()

    extracted = adapter_for(platform).extract_external_event_ids(
        payload, raw_body, platform.headers
    )
    assert extracted == [(expected_digest, platform.expected_account)]
    batch = await adapter_for(platform).parse_webhook(
        SESSION, TENANT_ID, CHANNEL_ACCOUNT_ID, payload, platform.headers
    )
    for event in batch.inbound_events:
        assert event.external_event_id == expected_digest


@pytest.mark.parametrize("slug", SLUGS)
async def test_digest_fallback_survives_a_reformatted_body(slug: str) -> None:
    """A pretty-printed vendor body must not change the event identity."""
    platform = platform_case(slug)
    payload = scrub_identifiers(platform.payload)
    canonical_digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    pretty_body = json.dumps(payload, indent=2, sort_keys=False).encode()

    extracted = adapter_for(platform).extract_external_event_ids(
        payload, pretty_body, platform.headers
    )
    assert extracted[0][0] == canonical_digest
    batch = await adapter_for(platform).parse_webhook(
        SESSION, TENANT_ID, CHANNEL_ACCOUNT_ID, payload, platform.headers
    )
    for event in batch.inbound_events:
        assert event.external_event_id == extracted[0][0]


@pytest.mark.parametrize("slug", SLUGS)
async def test_account_id_falls_back_to_the_payload(slug: str) -> None:
    platform = platform_case(slug)
    extracted = adapter_for(platform).extract_external_event_ids(
        platform.payload, platform.raw_body, {}
    )
    assert extracted[0][1] == platform.expected_account


# ---------------------------------------------------------------------------------
# inbound normalization
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
async def test_parse_webhook_normalizes_the_marketplace_buyer(slug: str) -> None:
    platform = platform_case(slug)
    event = (await parse(platform, platform.payload)).inbound_events[0]

    assert event.sender_identity_type == IdentityType.MARKETPLACE_BUYER_ID
    assert event.sender_identity_value == f"{slug}:{platform.expected_buyer}"
    assert event.external_thread_id == platform.expected_thread
    assert event.external_message_id
    assert event.sender_display_name
    assert event.conversation_mode == platform.conversation_mode
    assert event.extra_identities == list(platform.extra_identities)
    assert event.body_text == BUYER_TEXT
    assert event.content_type == MessageContentType.TEXT
    assert event.source_timestamp.tzinfo is not None
    assert event.direction.value == "inbound"
    assert event.sender_type.value == "customer"

    metadata = event.channel_metadata
    assert metadata["platform"] == slug
    assert metadata["buyer_id"] == platform.expected_buyer
    assert metadata["platform_thread_id"] == platform.expected_thread
    assert metadata["conversation_id"] == platform.expected_thread
    for key in ("order_id", "order_no"):
        assert key in metadata


async def test_parse_webhook_preserves_the_amazon_order_id() -> None:
    platform = platform_case("amazon_sp")
    event = (await parse(platform, platform.payload)).inbound_events[0]
    assert event.channel_metadata["order_id"] == "111-2223334-5556667"
    assert event.external_thread_id == "111-2223334-5556667"


async def test_parse_webhook_preserves_an_order_number() -> None:
    platform = platform_case("shopee")
    payload = {**platform.payload, "order_no": "SG-ORDER-77", "order_id": "sg-order-1"}
    event = (await parse(platform, payload)).inbound_events[0]
    assert event.channel_metadata["order_no"] == "SG-ORDER-77"
    assert event.channel_metadata["order_id"] == "sg-order-1"


async def test_parse_webhook_keeps_the_alibaba_b2b_envelope() -> None:
    platform = platform_case("alibaba_email")
    event = (await parse(platform, platform.payload)).inbound_events[0]
    assert event.conversation_mode == ConversationMode.B2B_INQUIRY
    assert event.subject == "RFQ 2000 pcs"
    assert (IdentityType.EMAIL, "buyer@example.com") in event.extra_identities
    assert event.country_hint == "US"


@pytest.mark.parametrize("slug", SLUGS)
async def test_parse_webhook_flags_carrier_stop_keywords(slug: str) -> None:
    platform = platform_case(slug)
    event = (await parse(platform, with_body(platform, "  Stop "))).inbound_events[0]
    assert event.is_stop_command is True
    assert (event.body_text or "").strip() == "Stop"


@pytest.mark.parametrize("slug", SLUGS)
async def test_parse_webhook_skips_ordinary_replies(slug: str) -> None:
    platform = platform_case(slug)
    event = (await parse(platform, with_body(platform, "Thanks, please send it"))).inbound_events[0]
    assert event.is_stop_command is False


@pytest.mark.parametrize("slug", SLUGS)
async def test_parse_webhook_ignores_seller_echoes(slug: str) -> None:
    platform = platform_case(slug)
    payload = {**platform.payload, "sender_role": "seller"}
    assert (await parse(platform, payload)).inbound_events == []


async def test_parse_webhook_ignores_a_shopee_self_echo() -> None:
    platform = platform_case("shopee")
    payload = {**platform.payload, "from_id": platform.expected_account}
    assert (await parse(platform, payload)).inbound_events == []


async def test_parse_webhook_ignores_a_shein_seller_message() -> None:
    platform = platform_case("shein")
    payload = {**platform.payload, "sender_type": "SELLER"}
    assert (await parse(platform, payload)).inbound_events == []


async def test_parse_webhook_ignores_a_sourceless_payload() -> None:
    platform = platform_case("shopee")
    payload = {
        "shop_id": "778899",
        "from_id": "sg-buyer-1",
        "conversation_id": "sg-conv-1",
        "message_id": "sg-empty-1",
    }
    assert (await parse(platform, payload)).inbound_events == []


async def test_parse_webhook_requires_a_buyer_id() -> None:
    platform = platform_case("shopee")
    payload = {
        "shop_id": "778899",
        "conversation_id": "sg-conv-1",
        "message_id": "sg-nobody-1",
        "content": {"text": "hello?"},
    }
    assert (await parse(platform, payload)).inbound_events == []


async def test_parse_webhook_falls_back_to_the_buyer_id_as_thread() -> None:
    platform = platform_case("shopee")
    payload = {
        "shop_id": "778899",
        "from_id": "sg-threadless-buyer",
        "message_id": "sg-threadless-1",
        "content": {"text": "hello?"},
    }
    event = (await parse(platform, payload)).inbound_events[0]
    assert event.external_thread_id == "sg-threadless-buyer"
    assert event.channel_metadata["platform_thread_id"] == "sg-threadless-buyer"


async def test_parse_webhook_keeps_the_buyer_email_as_an_extra_identity() -> None:
    platform = platform_case("shein")
    payload = {**platform.payload, "buyer_email": "Ana@Example.com"}
    event = (await parse(platform, payload)).inbound_events[0]
    assert (IdentityType.EMAIL, "Ana@Example.com") in event.extra_identities
    assert event.sender_identity_type == IdentityType.MARKETPLACE_BUYER_ID


@pytest.mark.parametrize("slug", SLUGS)
async def test_parse_webhook_captures_image_attachments(slug: str) -> None:
    platform = platform_case(slug)
    payload = {**platform.payload, "image_url": "https://cdn.example.com/chat/photo.png"}
    event = (await parse(platform, payload)).inbound_events[0]
    assert [attachment.external_url for attachment in event.attachments] == [
        "https://cdn.example.com/chat/photo.png"
    ]
    assert event.attachments[0].filename == "photo.png"
    assert event.attachments[0].mime_type == "image/png"
    assert event.content_type == MessageContentType.MEDIA


async def test_parse_webhook_reads_an_attachment_without_text() -> None:
    platform = platform_case("shopee")
    payload = {
        "shop_id": "778899",
        "from_id": "sg-buyer-9",
        "conversation_id": "sg-conv-9",
        "message_id": "sg-media-1",
        "attachments": [{"url": "https://cdn.example.com/chat/spec.pdf"}],
    }
    event = (await parse(platform, payload)).inbound_events[0]
    assert event.body_text is None
    assert event.attachments[0].external_url == "https://cdn.example.com/chat/spec.pdf"
    assert event.content_type == MessageContentType.MEDIA


@pytest.mark.parametrize("keyword", sorted(STOP_KEYWORDS))
def test_is_stop_command_accepts_every_carrier_keyword(keyword: str) -> None:
    assert is_stop_command(keyword) is True
    assert is_stop_command(f"  {keyword.lower()}  ") is True


@pytest.mark.parametrize("text", ["", None, "stop please", "STOPPED", "hi"])
def test_is_stop_command_rejects_partial_matches(text: str | None) -> None:
    assert is_stop_command(text) is False


# ---------------------------------------------------------------------------------
# outbound sends
# ---------------------------------------------------------------------------------


def mock_outbound(
    status_code: int = 200,
    body: Mapping[str, Any] | None = None,
    seen: list[httpx.Request] | None = None,
    error: type[Exception] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if error is not None:
            raise error("transport down")
        payload = dict(body or {})
        return httpx.Response(status_code, json=payload, request=request)

    return httpx.MockTransport(handler)


async def send(
    platform: Platform,
    transport: httpx.MockTransport,
    *,
    credentials: Mapping[str, Any] | None = None,
    request: OutboundMessageRequest | None = None,
) -> OutboundSendResult:
    adapter = build_marketplace_im_adapters(http_transport=transport)[platform.channel_type]
    return await adapter.send_message(
        SESSION,
        TENANT_ID,
        dict(platform.credentials if credentials is None else credentials),
        request or outbound_request(platform),
    )


@pytest.mark.parametrize("slug", SLUGS)
async def test_send_message_posts_a_signed_request(slug: str) -> None:
    platform = platform_case(slug)
    seen: list[httpx.Request] = []
    result = await send(
        platform, mock_outbound(200, {"data": {"message_id": f"out-{slug}"}}, seen)
    )

    assert result.success is True
    assert result.external_message_id == f"out-{slug}"
    assert result.error_code is None
    assert result.retryable is False
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path.endswith(platform.send_url_suffix) or request.url.path == (
        platform.send_url_suffix
    )
    for name in platform.send_params:
        assert request.url.params[name], f"{slug} is missing signed param {name}"
    for name in platform.send_headers:
        assert request.headers[name], f"{slug} is missing header {name}"
    assert OUTBOUND_TEXT.encode() in request.content


#: Platforms that carry the buyer id in the signed body or the URL path.
RECIPIENT_IN_REQUEST = (
    "shopee",
    "lazada",
    "temu",
    "shein",
    "aliexpress",
    "walmart",
    "ebay",
    "alibaba_email",
)


@pytest.mark.parametrize("slug", SLUGS)
async def test_send_message_strips_the_platform_prefix_from_the_recipient(slug: str) -> None:
    platform = platform_case(slug)
    seen: list[httpx.Request] = []
    await send(
        platform,
        mock_outbound(200, {"message_id": f"out-{slug}"}, seen),
        request=outbound_request(platform, recipient_id=f"{slug}:buyer-42"),
    )
    body = seen[0].content.decode()
    url = str(seen[0].url)
    # The composite inbox handle never reaches the platform (it is not a real id).
    assert f"{slug}:buyer-42" not in body
    assert f"{slug}%3Abuyer-42" not in url
    if slug in RECIPIENT_IN_REQUEST:
        assert "buyer-42" in body or "buyer-42" in url


async def test_shopee_signature_is_recomputable_from_the_wire_request() -> None:
    platform = platform_case("shopee")
    seen: list[httpx.Request] = []
    await send(platform, mock_outbound(200, {"message_id": "out-1"}, seen))

    query = seen[0].url.params
    signature = hmac.new(
        b"shopee-partner-key",
        (
            f"10001{platform.send_url_suffix}{query['timestamp']}"
            f"shopee-access-token778899"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert query["sign"] == signature
    assert seen[0].url.params["partner_id"] == "10001"


async def test_temu_signature_covers_the_json_body() -> None:
    platform = platform_case("temu")
    seen: list[httpx.Request] = []
    await send(platform, mock_outbound(200, {"message_id": "out-1"}, seen))

    query = dict(seen[0].url.params)
    signature = query.pop("sign")
    canonical = "".join(f"{key}{value}" for key, value in sorted(query.items()))
    expected = hashlib.md5(
        f"temu-app-secret{canonical}{seen[0].content.decode()}temu-app-secret".encode(),
        usedforsecurity=False,
    ).hexdigest().upper()
    assert signature == expected


async def test_send_message_honours_a_sandbox_base_url_override() -> None:
    platform = platform_case("shopee")
    seen: list[httpx.Request] = []
    credentials = {**platform.credentials, "base_url": "https://sandbox.example.com/"}
    await send(platform, mock_outbound(200, {"message_id": "out-1"}, seen), credentials=credentials)
    assert str(seen[0].url).startswith(
        "https://sandbox.example.com/api/v2/sellerchat/send_message?"
    )


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_send_message_is_retryable_when_the_platform_is_unavailable(
    slug: str, status_code: int
) -> None:
    platform = platform_case(slug)
    result = await send(platform, mock_outbound(status_code, {"error": "busy"}))
    assert result.success is False
    assert result.retryable is True
    assert result.error_code == status_code
    assert result.external_message_id is None


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("status_code", [400, 403])
async def test_send_message_is_terminal_on_a_client_error(
    slug: str, status_code: int
) -> None:
    platform = platform_case(slug)
    leaked = "should-never-appear-in-errors"
    result = await send(
        platform,
        mock_outbound(status_code, {"error": leaked, "token": platform.credentials}),
    )
    assert result.success is False
    assert result.retryable is False
    assert result.error_code == status_code
    assert leaked not in (result.error_message or "")
    for value in platform.credentials.values():
        assert value not in (result.error_message or "")


@pytest.mark.parametrize("slug", SLUGS)
async def test_send_message_fails_fast_without_credentials(slug: str) -> None:
    platform = platform_case(slug)
    seen: list[httpx.Request] = []
    result = await send(platform, mock_outbound(200, {}, seen), credentials={})
    assert result.success is False
    assert result.retryable is False
    assert result.error_code == 400
    assert result.external_message_id is None
    assert platform.slug in (result.error_message or "")
    assert "Missing required credentials" in (result.error_message or "")
    assert seen == []
    for value in platform.credentials.values():
        assert value not in (result.error_message or "")


@pytest.mark.parametrize("slug", SLUGS)
async def test_send_message_flags_a_transport_failure_as_retryable(slug: str) -> None:
    platform = platform_case(slug)
    result = await send(platform, mock_outbound(error=httpx.ConnectError))
    assert result.success is False
    assert result.retryable is True
    assert result.error_code == 503
    for value in platform.credentials.values():
        assert value not in (result.error_message or "")


async def test_send_message_reports_a_missing_thread_without_calling_the_platform() -> None:
    platform = platform_case("mercado_libre")
    seen: list[httpx.Request] = []
    request = OutboundMessageRequest(
        tenant_id=TENANT_ID,
        channel_account_id=CHANNEL_ACCOUNT_ID,
        channel_type=platform.channel_type,
        external_account_id=platform.expected_account,
        recipient_id="",
        conversation_id=uuid4(),
        message_id=uuid4(),
        body_text=OUTBOUND_TEXT,
        metadata={},
    )
    result = await send(platform, mock_outbound(200, {}, seen), request=request)
    assert result.success is False
    assert result.retryable is False
    assert result.error_code == 400
    assert seen == []


async def test_amazon_send_targets_the_order_messaging_path() -> None:
    platform = platform_case("amazon_sp")
    seen: list[httpx.Request] = []
    request = outbound_request(platform)
    request.metadata["amazon_message_type"] = "confirmDeliveryDetails"
    await send(platform, mock_outbound(200, {"messageId": "amazon-out-1"}, seen), request=request)
    assert seen[0].url.path.endswith(
        "/messaging/v1/orders/111-2223334-5556667/messages/confirmDeliveryDetails"
    )
    assert seen[0].headers["x-amz-access-token"] == "amazon-access-token"
    body = json.loads(seen[0].content)
    assert body["marketplaceIds"] == ["ATVPDKIKX0DER"]
    assert body["body"] == OUTBOUND_TEXT


async def test_send_message_uses_the_response_message_id_when_present() -> None:
    platform = platform_case("ozon")
    result = await send(platform, mock_outbound(200, {"message_id": "ozon-out-42"}))
    assert result.success is True
    assert result.external_message_id == "ozon-out-42"


async def test_send_message_falls_back_to_the_local_message_id() -> None:
    platform = platform_case("ozon")
    request = outbound_request(platform)
    result = await send(platform, mock_outbound(202, {}), request=request)
    assert result.success is True
    assert result.external_message_id == str(request.message_id)
