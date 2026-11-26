"""Buyer-IM messaging adapters for the global marketplace fleet.

Every marketplace that ships a buyer chat — Shopee Seller Chat, Lazada IM, Temu IM,
SHEIN IM, AliExpress IM, Ozon chat, Walmart Messages, Mercado Libre messages,
TikTok Shop customer service, eBay member messages, Amazon buyer-seller messaging and
Alibaba International TradeManager — exposes that chat through a *different* webhook and
a *different* signed API than its order/fulfillment feed. This module owns that whole
conversation surface for the twelve marketplaces the ERP onboards.

Design contract (identical for all twelve channels):

* ``verify_webhook_signature`` delegates to the platform's own verifier that the
  order-feed connectors already ship (single source of truth for HMAC shapes) and stays
  fail-closed: an empty secret, a missing header or a bad digest is ``False``.
* ``extract_external_event_ids`` and ``parse_webhook`` share one identifier engine, so the
  ``(external_event_id, external_account_id)`` pair the ingress worker uses to split a
  ``raw_webhook_events`` row is byte-identical to the pair persisted on the
  ``NormalizedInboundEvent``. ``filter_webhook_batch_for_row`` compares those strings
  exactly; any divergence would silently drop the event.
* ``send_message`` performs a real signed HTTP POST against the platform endpoint (with a
  ``base_url``/``endpoint_url`` override for sandboxes and tests) and maps every failure
  onto the retry contract: ``429``/``5xx``/transport errors are retryable, ``4xx`` and
  missing credentials are terminal. Error messages never echo credentials or response
  bodies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Final
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.ecommerce.alibaba_intl import (
    sign_alibaba_iop_request,
    verify_alibaba_iop_webhook_signature,
)
from erp_core.channels.ecommerce.aliexpress import (
    sign_aliexpress_request,
    verify_aliexpress_webhook,
)
from erp_core.channels.ecommerce.amazon_sp import AmazonSpApiAdapter
from erp_core.channels.ecommerce.ebay import EBAY_SANDBOX_BASE_URL, EbayConnectorAdapter
from erp_core.channels.ecommerce.lazada import sign_lazada_request, verify_lazada_webhook
from erp_core.channels.ecommerce.mercado_libre import verify_mercado_libre_webhook
from erp_core.channels.ecommerce.ozon import verify_ozon_webhook
from erp_core.channels.ecommerce.shein import sign_shein_request, verify_shein_webhook
from erp_core.channels.ecommerce.shopee import sign_shopee_request, verify_shopee_webhook
from erp_core.channels.ecommerce.temu import sign_temu_request, verify_temu_webhook
from erp_core.channels.ecommerce.tiktok_shop import (
    sign_tiktok_request,
    verify_tiktok_webhook_signature,
)
from erp_core.channels.ecommerce.walmart import verify_walmart_webhook
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    IdentityType,
    MessageContentType,
)

#: Every marketplace with a buyer-IM surface the ERP speaks.
MARKETPLACE_IM_CHANNELS: Final[tuple[ChannelType, ...]] = (
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
)

#: Channel slugs accepted by :func:`is_marketplace_im_event`.
MARKETPLACE_IM_SLUGS: Final[frozenset[str]] = frozenset(
    channel.value for channel in MARKETPLACE_IM_CHANNELS
)

#: Internal header the ingress router sets when a route is dedicated to buyer IM.
IM_EVENT_HEADER: Final[str] = "x-erp-im-event"
#: Absolute callback url a marketplace signed its push against (Shopee HMAC shape).
CALLBACK_URL_HEADER: Final[str] = "x-erp-callback-url"

_TRUTHY: Final[frozenset[str]] = frozenset({"true", "1", "yes"})

#: Carrier opt-out keywords — a marketplace buyer answering STOP must trip suppression.
STOP_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
)

#: Push topics / event types that are genuine buyer-IM deliveries.
IM_TOPIC_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "buyer_chat_message",
        "sellerchat_message",
        "chat_message",
        "buyer_message",
        "im_message_received",
        "chat.message.created",
        "chat.message",
        "buyer_inquiry",
        "im_message",
        "buyer_chat",
        "type_new_message",
        "new_message",
        "messages",
        "questions",
        "inbound_message",
        "customer_service_message",
        "member_message",
        "inquiry_message",
        "trademanager_message",
    }
)

#: Order / fulfillment topics that must never be mistaken for a chat message.
ORDER_TOPIC_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "orders/create",
        "order_status_update",
        "posting_status_changed",
        "orders/updated",
        "order.created",
        "order.updated",
        "order_created",
        "order_status",
        "orderstatuschanged",
        "package_status_changed",
        "shipping_update",
        "shipment_update",
        "fulfillment_update",
        "tracking_update",
        "awb_status_changed",
        "orders",
        "order",
        "returns",
        "refunds",
    }
)

#: Fragments that mark a topic as an order-feed delivery even when it is not enumerated.
_ORDER_TOPIC_FRAGMENTS: Final[tuple[str, ...]] = (
    "order",
    "posting_status",
    "fulfillment",
    "shipment",
    "shipping",
    "tracking",
    "delivery_status",
    "return",
    "refund",
)

#: Per-platform topic headers (``connectors_router.TOPIC_HEADERS`` shape).
TOPIC_HEADER_BY_SLUG: Final[dict[str, str]] = {
    "shopee": "x-shopee-topic",
    "lazada": "x-lazada-topic",
    "temu": "x-temu-topic",
    "shein": "x-shein-topic",
    "aliexpress": "x-aliexpress-topic",
    "ozon": "x-ozon-topic",
    "walmart": "x-walmart-topic",
    "mercado_libre": "x-meli-topic",
    "tiktok_shop": "x-tts-topic",
}
#: Generic topic headers accepted for every marketplace.
GENERIC_TOPIC_HEADERS: Final[tuple[str, ...]] = ("topic", "x-erp-topic")

#: Payload fields that can carry a topic / event type / push code.
TOPIC_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "event_type",
    "topic",
    "push_code",
    "code",
    "message_type",
    "type",
    "NotificationType",
    "notification_type",
)

#: Shopee's documented buyer-chat push code.
_SHOPEE_CHAT_PUSH_CODE: Final = 10

#: Container keys searched (breadth-first, depth-2) when normalizing a delivery.
_CONTAINER_KEYS: Final[tuple[str, ...]] = (
    "data",
    "payload",
    "message",
    "msg",
    "chat",
    "conversation",
    "sender",
    "from",
    "buyer",
    "buyer_info",
    "user",
    "customer",
    "order",
    "notification",
    "content",
    "extra",
)

#: Sender roles that mean "our own outbound message echoed back".
_OUTBOUND_ROLES: Final[frozenset[str]] = frozenset(
    {
        "seller",
        "merchant",
        "shop",
        "shopkeeper",
        "bot",
        "platform",
        "agent",
        "staff",
        "service",
        "system",
        "official",
        "assistant",
        "outbound",
        "out",
        "seller_to_buyer",
    }
)

_CANONICAL_THREAD_KEYS: Final[tuple[str, ...]] = (
    "conversation_id",
    "conversationId",
    "conversationID",
    "chat_id",
    "chatId",
    "thread_id",
    "threadId",
    "session_id",
    "sessionId",
    "im_session_id",
    "pack_id",
    "packId",
    "inquiry_id",
    "inquiryId",
    "trademanager_id",
)
_CANONICAL_ORDER_KEYS: Final[tuple[str, ...]] = (
    "order_id",
    "orderId",
    "order_no",
    "orderNo",
    "order_number",
    "orderNumber",
    "trade_id",
    "tradeId",
    "purchase_order_id",
    "purchaseOrderId",
)
_CANONICAL_BUYER_KEYS: Final[tuple[str, ...]] = (
    "buyer_id",
    "buyerId",
    "buyer_user_id",
    "buyerUserId",
    "buyer_open_id",
    "buyerOpenId",
    "sender_id",
    "senderId",
    "from_id",
    "fromId",
    "from_user_id",
    "fromUserId",
    "customer_id",
    "customerId",
    "member_id",
    "memberId",
)

_CANONICAL_ROLE_KEYS: Final[tuple[str, ...]] = (
    "sender_role",
    "sender_type",
    "from_type",
    "fromType",
    "author_type",
    "authorType",
    "role",
    "direction",
    "msg_direction",
)
_CANONICAL_BODY_KEYS: Final[tuple[str, ...]] = (
    "text",
    "body_text",
    "message_text",
    "messageText",
    "content_text",
    "contentText",
    "msg_text",
    "text_body",
    "content",
    "message",
    "msg",
    "body",
    "question",
    "inquiry_message",
)
_CANONICAL_NAME_KEYS: Final[tuple[str, ...]] = (
    "buyer_name",
    "buyerName",
    "buyer_nickname",
    "buyer_nick",
    "nickname",
    "display_name",
    "displayName",
    "sender_name",
    "senderName",
    "from_name",
    "customer_name",
    "user_name",
    "userName",
    "name",
)
_CANONICAL_EMAIL_KEYS: Final[tuple[str, ...]] = (
    "buyer_email",
    "buyerEmail",
    "email",
    "from_email",
    "sender_email",
    "customer_email",
    "contact_email",
)
_CANONICAL_SUBJECT_KEYS: Final[tuple[str, ...]] = (
    "subject",
    "inquiry_subject",
    "inquirySubject",
    "title",
)
_CANONICAL_TIMESTAMP_KEYS: Final[tuple[str, ...]] = (
    "source_timestamp",
    "create_time",
    "createTime",
    "created_at",
    "createdAt",
    "created_timestamp",
    "send_time",
    "msg_time",
    "event_time",
    "EventTime",
    "timestamp",
    "time",
)
_CANONICAL_EVENT_ID_KEYS: Final[tuple[str, ...]] = (
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
    "id",
)
_CANONICAL_COUNTRY_KEYS: Final[tuple[str, ...]] = (
    "country",
    "country_code",
    "countryCode",
    "buyer_country",
    "buyer_country_code",
    "ship_country",
)
_ATTACHMENT_URL_KEYS: Final[tuple[str, ...]] = (
    "image_url",
    "imageUrl",
    "media_url",
    "mediaUrl",
    "attachment_url",
    "attachmentUrl",
    "file_url",
    "fileUrl",
    "download_url",
    "downloadUrl",
    "picture_url",
    "pictureUrl",
)
_ATTACHMENT_CONTAINER_KEYS: Final[tuple[str, ...]] = (
    "attachments",
    "images",
    "image_list",
    "media",
    "files",
)


# ---------------------------------------------------------------------------------
# channel slugs & topic classification
# ---------------------------------------------------------------------------------


def _channel_slug(channel_type: str) -> str:
    """Normalize ``"ChannelType.SHOPEE"``/``"SHOPEE"``/``" shopee "`` to ``"shopee"``."""
    slug = str(channel_type).strip().lower()
    if slug.startswith("channeltype."):
        slug = slug.split(".", 1)[1]
    return slug


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).strip().lower(): str(value) for key, value in headers.items()}


def _scopes(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Payload scopes searched breadth-first (depth 2, cycle-safe, bounded)."""
    scopes: list[Mapping[str, Any]] = [payload]
    index = 0
    while index < len(scopes) and index < 6:
        scope = scopes[index]
        index += 1
        for key in _CONTAINER_KEYS:
            nested = scope.get(key)
            if isinstance(nested, Mapping) and not any(nested is seen for seen in scopes):
                scopes.append(nested)
    return scopes


def _first_text(scopes: list[Mapping[str, Any]], keys: tuple[str, ...]) -> str | None:
    """First non-empty scalar for ``keys`` (key-major, then scope order)."""
    for key in keys:
        for scope in scopes:
            value = scope.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
            elif isinstance(value, bool):
                continue
            elif isinstance(value, (int, float)):
                return str(value)
    return None


def _actor_scope_value(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    """Look inside ``sender``/``from``/``buyer``/``customer`` actor objects."""
    for key in ("sender", "from", "buyer", "customer", "author", "user"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            found = _first_text([nested], keys)
            if found:
                return found
    return None


def _actor_value(scopes: list[Mapping[str, Any]], keys: tuple[str, ...]) -> str | None:
    """Actor lookup across every nesting level of the payload."""
    for scope in scopes:
        found = _actor_scope_value(scope, keys)
        if found:
            return found
    return None


def _topic_values(
    slug: str, headers: Mapping[str, str], payload: Mapping[str, Any]
) -> list[str]:
    """Every topic-ish value this delivery carries, header first."""
    lowered = _lower_headers(headers)
    values: list[str] = []
    names = list(GENERIC_TOPIC_HEADERS)
    platform_header = TOPIC_HEADER_BY_SLUG.get(slug)
    if platform_header:
        names.insert(0, platform_header)
    for name in names:
        header_value = lowered.get(name, "").strip()
        if header_value:
            values.append(header_value)
    scopes = _scopes(payload)
    for name in TOPIC_PAYLOAD_FIELDS:
        for scope in scopes:
            raw = scope.get(name)
            if isinstance(raw, str) and raw.strip():
                values.append(raw.strip())
            elif isinstance(raw, int) and not isinstance(raw, bool):
                values.append(str(raw))
            elif isinstance(raw, (list, tuple)):
                values.extend(str(item) for item in raw if isinstance(item, str) and item)
    return values


def _is_order_topic(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized in ORDER_TOPIC_MARKERS:
        return True
    return any(fragment in normalized for fragment in _ORDER_TOPIC_FRAGMENTS)


def _is_im_topic(value: str) -> bool:
    return value.strip().lower() in IM_TOPIC_MARKERS


def _has_chat_message_shape(payload: Mapping[str, Any]) -> bool:
    """A payload carrying a thread id *and* message content is a chat message."""
    scopes = _scopes(payload)
    thread = _first_text(scopes, _CANONICAL_THREAD_KEYS)
    if not thread:
        return False
    if _first_text(scopes, _CANONICAL_BODY_KEYS):
        return True
    for scope in scopes:
        for key in _ATTACHMENT_URL_KEYS:
            value = scope.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return True
        container = scope.get("attachments") or scope.get("images")
        if isinstance(container, list) and container:
            return True
    return False


def is_marketplace_im_event(
    channel_type: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
) -> bool:
    """True only for a genuine buyer-IM delivery on a marketplace chat channel.

    Order and fulfillment pushes share the same endpoint family, so a chat-shaped payload
    is never enough on its own: an order topic always wins and returns ``False``.
    """
    slug = _channel_slug(channel_type)
    if slug not in MARKETPLACE_IM_SLUGS:
        return False
    lowered = _lower_headers(headers)
    if lowered.get(IM_EVENT_HEADER, "").strip().lower() in _TRUTHY:
        return True
    topics = _topic_values(slug, headers, payload)
    if any(_is_order_topic(topic) for topic in topics):
        return False
    if any(_is_im_topic(topic) for topic in topics):
        return True
    if any(topic.strip() == str(_SHOPEE_CHAT_PUSH_CODE) for topic in topics):
        return True
    return _has_chat_message_shape(payload)


def is_stop_command(body_text: str | None) -> bool:
    """Carrier-style opt-out keyword detection (``STOP``/``END``/``UNSUBSCRIBE``...)."""
    if not body_text:
        return False
    return body_text.strip().upper() in STOP_KEYWORDS


# ---------------------------------------------------------------------------------
# outbound request plumbing
# ---------------------------------------------------------------------------------

_IMAGE_MIME_BY_SUFFIX: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _attachment_mime(url: str) -> str:
    path = url.lower().split("?", 1)[0].split("#", 1)[0]
    for suffix, mime in _IMAGE_MIME_BY_SUFFIX.items():
        if path.endswith(suffix):
            return mime
    return "application/octet-stream"


@dataclass(frozen=True, slots=True)
class _SendCall:
    """A fully signed outbound HTTP request."""

    method: str
    url: str
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class _SendFailure:
    """A terminal pre-flight failure (missing credentials, unusable request)."""

    error_code: int
    error_message: str


def _text_credential(credentials: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = credentials.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _base_url(credentials: Mapping[str, Any], default: str) -> str:
    """Sandbox/mock override, then the platform default host."""
    return (_text_credential(credentials, "endpoint_url", "base_url") or default).rstrip("/")


def _json_body(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _segment(value: str) -> str:
    return quote(value.strip(), safe="")


def _strip_platform_prefix(value: str) -> str:
    head, separator, tail = value.partition(":")
    if separator and tail and head.strip().lower() in MARKETPLACE_IM_SLUGS:
        return tail.strip()
    return value.strip()


def _recipient(request: OutboundMessageRequest) -> str:
    return _strip_platform_prefix(request.recipient_id)


def _thread(request: OutboundMessageRequest) -> str:
    for key in (
        "conversation_id",
        "chat_id",
        "thread_id",
        "platform_thread_id",
        "session_id",
        "pack_id",
        "order_id",
        "external_thread_id",
    ):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if request.in_reply_to and request.in_reply_to.strip():
        return request.in_reply_to.strip()
    return _recipient(request)


def _order_id_for(request: OutboundMessageRequest) -> str:
    for key in ("order_id", "order_no", "order_number"):
        value = request.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("order_id", "order_no"):
        value = request.metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _message_text(request: OutboundMessageRequest) -> str:
    return (request.body_text or "").strip()


def _missing(platform: ChannelType, names: list[str]) -> _SendFailure:
    return _SendFailure(
        error_code=400,
        error_message=(
            f"Missing required credentials for {platform.value} buyer messaging: "
            + ", ".join(names)
        ),
    )


# ---------------------------------------------------------------------------------
# base adapter
# ---------------------------------------------------------------------------------


class _BaseMarketplaceImAdapter(MessagingChannelAdapter):
    """Shared buyer-IM contract: one signed webhook in, one signed message out."""

    platform: ClassVar[ChannelType]
    channel_types: ClassVar[tuple[ChannelType, ...]]

    #: Platform headers carrying the seller/shop/mall account id.
    account_headers: ClassVar[tuple[str, ...]] = ()
    #: Payload fallbacks for the account id when no header is present.
    account_payload_keys: ClassVar[tuple[str, ...]] = (
        "shop_id",
        "seller_id",
        "mall_id",
        "client_id",
        "store_id",
        "account_id",
    )
    #: Envelope mode for the inbound conversation.
    conversation_mode: ClassVar[ConversationMode] = ConversationMode.B2C_SUPPORT
    #: Default API host for outbound sends.
    api_base: ClassVar[str] = ""
    #: Path appended to the resolved base url.
    api_path: ClassVar[str] = ""
    #: Overridable identifier shapes.
    buyer_keys: ClassVar[tuple[str, ...]] = _CANONICAL_BUYER_KEYS
    thread_keys: ClassVar[tuple[str, ...]] = _CANONICAL_THREAD_KEYS
    body_keys: ClassVar[tuple[str, ...]] = _CANONICAL_BODY_KEYS
    role_keys: ClassVar[tuple[str, ...]] = _CANONICAL_ROLE_KEYS

    #: Per-request timeout for outbound sends (seconds).
    request_timeout: ClassVar[float] = 20.0

    def __init__(self, *, http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http_transport = http_transport

    # ------------------------------------------------------------ verification

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        raise NotImplementedError

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        if not secret:
            return False
        try:
            return bool(self._verify(raw_body, headers, secret))
        except (TypeError, ValueError, KeyError):
            return False

    # ------------------------------------------------------------ identifiers

    def _account_id(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        lowered = _lower_headers(headers)
        for name in self.account_headers:
            value = lowered.get(name, "").strip()
            if value:
                return value
        return _first_text(_scopes(payload), self.account_payload_keys) or ""

    def _event_id(self, payload: Mapping[str, Any], raw_body: bytes | None = None) -> str:
        """Platform message id, else a digest that both call sites derive identically.

        ``parse_webhook`` never receives the raw bytes, so the digest fallback covers the
        canonical JSON form of the payload. When the vendor body already *is* that
        canonical form (compact, sorted keys) the digest is exactly
        ``sha256(raw_body)``; for any other formatting the canonical digest still gives
        ``extract_external_event_ids`` and ``parse_webhook`` the same row identity.
        """
        found = _first_text(_scopes(payload), _CANONICAL_EVENT_ID_KEYS)
        if found:
            return found
        canonical = _json_body(payload)
        if raw_body is not None and raw_body.strip() == canonical:
            return hashlib.sha256(raw_body).hexdigest()
        return hashlib.sha256(canonical).hexdigest()

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        """The exact ``(external_event_id, external_account_id)`` row identity."""
        return [(self._event_id(payload, raw_body), self._account_id(payload, headers))]

    # ------------------------------------------------------------ normalization

    def _is_echo(self, payload: Mapping[str, Any], account_id: str) -> bool:
        scopes = _scopes(payload)
        role = (_first_text(scopes, self.role_keys) or "").strip().lower()
        if not role:
            role = (
                _actor_value(scopes, ("type", "role", "sender_type", "senderType")) or ""
            ).strip().lower()
        if role in _OUTBOUND_ROLES:
            return True
        sender = _first_text(
            scopes, ("from_id", "fromId", "sender_id", "senderId", "author_id", "authorId")
        )
        return bool(sender and account_id and sender == account_id)

    def _attachments(self, payload: Mapping[str, Any]) -> list[NormalizedAttachment]:
        urls: list[str] = []

        def add(url: Any) -> None:
            if isinstance(url, str):
                candidate = url.strip()
                if candidate.startswith(("http://", "https://")) and candidate not in urls:
                    urls.append(candidate)

        for scope in _scopes(payload):
            for key in _ATTACHMENT_URL_KEYS:
                add(scope.get(key))
            for key in _ATTACHMENT_CONTAINER_KEYS:
                container = scope.get(key)
                items: list[Any] = list(container) if isinstance(container, list) else []
                if container is not None and not isinstance(container, list):
                    items = [container]
                for item in items:
                    if isinstance(item, str):
                        add(item)
                    elif isinstance(item, Mapping):
                        add(
                            _first_text(
                                [item],
                                (
                                    "url",
                                    "image_url",
                                    "imageUrl",
                                    "media_url",
                                    "mediaUrl",
                                    "download_url",
                                    "downloadUrl",
                                    "file_url",
                                    "fileUrl",
                                    "src",
                                ),
                            )
                        )
        return [
            NormalizedAttachment(
                filename=url.rsplit("/", 1)[-1].split("?", 1)[0] or "attachment",
                mime_type=_attachment_mime(url),
                external_url=url,
            )
            for url in urls
        ]

    def _source_timestamp(self, payload: Mapping[str, Any]) -> datetime:
        raw: Any = None
        for key in _CANONICAL_TIMESTAMP_KEYS:
            for scope in _scopes(payload):
                value = scope.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) or (
                    isinstance(value, str) and value.strip()
                ):
                    raw = value
                    break
            if raw is not None:
                break
        if raw is None:
            return datetime.now(UTC)
        if isinstance(raw, str):
            text = raw.strip()
            candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            try:
                raw = float(text)
            except ValueError:
                return datetime.now(UTC)
        seconds = float(raw)
        if seconds > 1e14:  # microseconds
            seconds /= 1_000_000
        elif seconds > 1e11:  # milliseconds
            seconds /= 1_000
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return datetime.now(UTC)

    def _country_hint(self, payload: Mapping[str, Any]) -> str | None:
        candidate = (
            _first_text(_scopes(payload), _CANONICAL_COUNTRY_KEYS) or ""
        ).strip().upper()
        if len(candidate) == 2 and candidate.isascii() and candidate.isalpha():
            return candidate
        return None

    def _build_event(
        self, payload: Mapping[str, Any], raw_body: bytes | None, headers: Mapping[str, str]
    ) -> NormalizedInboundEvent | None:
        account_id = self._account_id(payload, headers)
        if self._is_echo(payload, account_id):
            return None
        scopes = _scopes(payload)
        buyer_id = _first_text(scopes, self.buyer_keys) or _actor_value(
            scopes, ("id", "user_id", "userId", "buyer_id", "open_id", "member_id")
        )
        if not buyer_id:
            return None
        body_text = _first_text(scopes, self.body_keys)
        attachments = self._attachments(payload)
        if not body_text and not attachments:
            return None

        order_id = _first_text(scopes, _CANONICAL_ORDER_KEYS)
        order_no = _first_text(
            scopes, ("order_no", "orderNo", "order_number", "orderNumber", "trade_no")
        )
        conversation_id = _first_text(
            scopes, ("conversation_id", "conversationId", "chat_id", "chatId")
        )
        thread = (
            _first_text(scopes, self.thread_keys) or order_id or conversation_id or buyer_id
        )
        email = _first_text(scopes, _CANONICAL_EMAIL_KEYS)
        display_name = (
            _first_text(scopes, _CANONICAL_NAME_KEYS)
            or _actor_value(scopes, _CANONICAL_NAME_KEYS)
            or (email.split("@", 1)[0] if email else f"{self.platform.value} buyer {buyer_id}")
        )
        subject = _first_text(scopes, _CANONICAL_SUBJECT_KEYS)
        event_id = self._event_id(payload, raw_body)
        message_id = _first_text(
            scopes, ("message_id", "messageId", "msg_id", "msgId", "event_id", "eventId")
        )

        metadata: dict[str, Any] = {
            "platform": self.platform.value,
            "external_account_id": account_id,
            "order_id": order_id,
            "order_no": order_no,
            "buyer_id": buyer_id,
            "conversation_id": conversation_id or thread,
            "platform_thread_id": thread,
        }
        if subject:
            metadata["subject"] = subject

        return NormalizedInboundEvent(
            external_event_id=event_id,
            external_account_id=account_id,
            external_message_id=message_id or event_id,
            external_thread_id=thread,
            subject=subject,
            sender_identity_type=IdentityType.MARKETPLACE_BUYER_ID,
            sender_identity_value=f"{self.platform.value}:{buyer_id}",
            sender_display_name=display_name,
            country_hint=self._country_hint(payload),
            content_type=MessageContentType.MEDIA if attachments else MessageContentType.TEXT,
            body_text=body_text,
            attachments=attachments,
            source_timestamp=self._source_timestamp(payload),
            conversation_mode=self.conversation_mode,
            channel_metadata=metadata,
            extra_identities=[(IdentityType.EMAIL, email)] if email else [],
            is_stop_command=is_stop_command(body_text),
        )

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        del session, tenant_id, channel_account_id
        event = self._build_event(payload, None, headers)
        return NormalizedWebhookBatch(inbound_events=[event] if event else [])

    # ------------------------------------------------------------ outbound

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        raise NotImplementedError

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        del session, tenant_id
        prepared = self._prepare_send(credentials, request)
        if isinstance(prepared, _SendFailure):
            return OutboundSendResult(
                success=False,
                error_code=prepared.error_code,
                error_message=prepared.error_message,
                retryable=False,
            )
        try:
            async with httpx.AsyncClient(
                transport=self.http_transport, timeout=httpx.Timeout(self.request_timeout)
            ) as client:
                response = await client.request(
                    prepared.method,
                    prepared.url,
                    params=prepared.params or None,
                    headers=prepared.headers,
                    content=prepared.body,
                )
        except httpx.HTTPError:
            return OutboundSendResult(
                success=False,
                error_code=503,
                error_message=f"{self.platform.value} buyer messaging endpoint is unreachable",
                retryable=True,
            )
        return self._send_result(response, request)

    def _send_result(
        self, response: httpx.Response, request: OutboundMessageRequest
    ) -> OutboundSendResult:
        status = response.status_code
        if status == 429 or status >= 500:
            return OutboundSendResult(
                success=False,
                error_code=status,
                error_message=(
                    f"{self.platform.value} buyer messaging API is rate limited or "
                    f"unavailable (HTTP {status})"
                ),
                retryable=True,
            )
        if status >= 400:
            return OutboundSendResult(
                success=False,
                error_code=status,
                error_message=(
                    f"{self.platform.value} buyer messaging API rejected the message "
                    f"(HTTP {status})"
                ),
                retryable=False,
            )
        return OutboundSendResult(
            success=True, external_message_id=self._outbound_message_id(response, request)
        )

    def _outbound_message_id(
        self, response: httpx.Response, request: OutboundMessageRequest
    ) -> str:
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if isinstance(body, Mapping):
            found = _first_text(_scopes(body), _CANONICAL_EVENT_ID_KEYS)
            if found:
                return found
        return str(request.message_id)


# ---------------------------------------------------------------------------------
# 1. Shopee Seller Chat
# ---------------------------------------------------------------------------------


class ShopeeImAdapter(_BaseMarketplaceImAdapter):
    """Shopee Seller Chat (``/api/v2/sellerchat/send_message``)."""

    platform = ChannelType.SHOPEE
    channel_types = (ChannelType.SHOPEE,)
    account_headers = ("x-shopee-shop-id",)
    account_payload_keys = ("shop_id", "shopId")
    buyer_keys = ("from_id", "fromId", "buyer_id", "buyerId", "sender_id", "senderId")
    api_base = "https://partner.shopeemobile.com"
    api_path = "/api/v2/sellerchat/send_message"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        callback_url = _lower_headers(headers).get(CALLBACK_URL_HEADER, "").strip()
        return verify_shopee_webhook(
            raw_body, headers, secret, callback_url=callback_url or None
        )

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        partner_id = _text_credential(credentials, "partner_id")
        partner_key = _text_credential(credentials, "partner_key")
        access_token = _text_credential(credentials, "access_token")
        shop_id = _text_credential(credentials, "shop_id")
        missing = [
            name
            for name, value in (
                ("partner_id", partner_id),
                ("partner_key", partner_key),
                ("access_token", access_token),
                ("shop_id", shop_id),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        timestamp = int(time.time())
        params = {
            "partner_id": partner_id,
            "timestamp": str(timestamp),
            "sign": sign_shopee_request(
                partner_id=partner_id,
                partner_key=partner_key,
                api_path=self.api_path,
                timestamp=timestamp,
                access_token=access_token,
                shop_id=shop_id,
            ),
            "access_token": access_token,
            "shop_id": shop_id,
        }
        payload = {
            "to_id": _recipient(request),
            "conversation_id": _thread(request),
            "message_type": "text",
            "content": {"text": _message_text(request)},
        }
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            params=params,
            headers={"content-type": "application/json"},
            body=_json_body(payload),
        )


# ---------------------------------------------------------------------------------
# 2. Lazada IM
# ---------------------------------------------------------------------------------


class LazadaImAdapter(_BaseMarketplaceImAdapter):
    """Lazada IM (``/im/message/send``, Lazada IOP HMAC signature)."""

    platform = ChannelType.LAZADA
    channel_types = (ChannelType.LAZADA,)
    account_headers = ("x-lazada-seller-id",)
    account_payload_keys = ("seller_id", "sellerId", "shop_id", "shopId")
    buyer_keys = ("buyer_id", "buyerId", "from_id", "sender_id", "senderId", "buyer_user_id")
    api_base = "https://api.lazada.com/rest"
    api_path = "/im/message/send"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_lazada_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        app_key = _text_credential(credentials, "app_key")
        app_secret = _text_credential(credentials, "app_secret")
        access_token = _text_credential(credentials, "access_token")
        missing = [
            name
            for name, value in (
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("access_token", access_token),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        params: dict[str, str] = {
            "app_key": app_key,
            "access_token": access_token,
            "sign_method": "sha256",
            "timestamp": str(int(time.time() * 1000)),
        }
        params["sign"] = sign_lazada_request(params, app_secret)
        payload = {
            "session_id": _thread(request),
            "buyer_id": _recipient(request),
            "message_type": "text",
            "content": {"text": _message_text(request)},
        }
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            params=params,
            headers={"content-type": "application/json"},
            body=_json_body(payload),
        )


# ---------------------------------------------------------------------------------
# 3. Temu IM
# ---------------------------------------------------------------------------------


class TemuImAdapter(_BaseMarketplaceImAdapter):
    """Temu open-platform router (``bg.im.message.send``)."""

    platform = ChannelType.TEMU
    channel_types = (ChannelType.TEMU,)
    account_headers = ("x-temu-mall-id",)
    account_payload_keys = ("mall_id", "mallId", "shop_id", "shopId")
    buyer_keys = ("buyer_id", "buyerId", "from_user_id", "fromUserId", "sender_id")
    thread_keys = ("session_id", "sessionId", "conversation_id", "chat_id") + _CANONICAL_THREAD_KEYS
    api_base = "https://openapi-b-us.temu.com"
    api_path = "/openapi/router"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_temu_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        app_key = _text_credential(credentials, "app_key")
        app_secret = _text_credential(credentials, "app_secret")
        access_token = _text_credential(credentials, "access_token")
        mall_id = _text_credential(credentials, "mall_id")
        missing = [
            name
            for name, value in (
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("access_token", access_token),
                ("mall_id", mall_id),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        body = _json_body(
            {
                "mall_id": mall_id,
                "session_id": _thread(request),
                "to_user_id": _recipient(request),
                "message_type": "text",
                "content": {"text": _message_text(request)},
            }
        )
        params: dict[str, str] = {
            "type": "bg.im.message.send",
            "app_key": app_key,
            "access_token": access_token,
            "timestamp": str(int(time.time())),
            "data_type": "JSON",
        }
        params["sign"] = sign_temu_request(app_secret, params, body=body.decode("utf-8"))
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            params=params,
            headers={"content-type": "application/json"},
            body=body,
        )


# ---------------------------------------------------------------------------------
# 4. SHEIN IM
# ---------------------------------------------------------------------------------


class SheinImAdapter(_BaseMarketplaceImAdapter):
    """SHEIN open API IM (``/open-api/im/message/send``)."""

    platform = ChannelType.SHEIN
    channel_types = (ChannelType.SHEIN,)
    account_headers = ("x-shein-seller-id",)
    account_payload_keys = ("seller_id", "sellerId", "supplier_id", "shop_id")
    buyer_keys = ("buyer_id", "buyerId", "from_user_id", "sender_id", "member_id")
    api_base = "https://openapi.sheincorp.com"
    api_path = "/open-api/im/message/send"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_shein_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        open_key_id = _text_credential(credentials, "open_key_id", "app_id", "open_key")
        secret_key = _text_credential(credentials, "secret_key", "app_secret")
        seller_id = _text_credential(credentials, "seller_id", "supplier_id")
        missing = [
            name
            for name, value in (
                ("open_key_id", open_key_id),
                ("secret_key", secret_key),
                ("seller_id", seller_id),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        timestamp = str(int(time.time() * 1000))
        body = _json_body(
            {
                "seller_id": seller_id,
                "conversation_id": _thread(request),
                "buyer_id": _recipient(request),
                "message_type": "text",
                "content": _message_text(request),
            }
        )
        signature = sign_shein_request(
            app_id=open_key_id,
            secret_key=secret_key,
            api_path=self.api_path,
            timestamp=timestamp,
            body_str=body.decode("utf-8"),
        )
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            headers={
                "content-type": "application/json",
                "x-shein-open-key-id": open_key_id,
                "x-shein-timestamp": timestamp,
                "x-shein-signature": signature,
            },
            body=body,
        )


# ---------------------------------------------------------------------------------
# 5. AliExpress IM
# ---------------------------------------------------------------------------------


class AliExpressImAdapter(_BaseMarketplaceImAdapter):
    """AliExpress IM over the Alibaba TOP gateway (``aliexpress.im.message.send``)."""

    platform = ChannelType.ALIEXPRESS
    channel_types = (ChannelType.ALIEXPRESS,)
    account_headers = ("x-aliexpress-seller-id",)
    account_payload_keys = ("seller_id", "sellerId", "shop_id", "user_id")
    buyer_keys = ("buyer_id", "buyerId", "buyer_login_id", "sender_id", "from_id")
    api_base = "https://api-sg.aliexpress.com/sync"
    api_path = ""

    #: TOP method name carried in the signed parameter map.
    method_name: ClassVar[str] = "aliexpress.im.message.send"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_aliexpress_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        app_key = _text_credential(credentials, "app_key")
        app_secret = _text_credential(credentials, "app_secret")
        access_token = _text_credential(credentials, "access_token")
        missing = [
            name
            for name, value in (
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("access_token", access_token),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        params: dict[str, str] = {
            "method": self.method_name,
            "app_key": app_key,
            "session": access_token,
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "v": "2.0",
            "sign_method": "sha256",
        }
        params["sign"] = sign_aliexpress_request(self.method_name, params, app_secret)
        payload = {
            "buyer_id": _recipient(request),
            "session_id": _thread(request),
            "message_type": "text",
            "content": {"text": _message_text(request)},
        }
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            params=params,
            headers={"content-type": "application/json"},
            body=_json_body(payload),
        )


# ---------------------------------------------------------------------------------
# 6. Ozon chat
# ---------------------------------------------------------------------------------


class OzonImAdapter(_BaseMarketplaceImAdapter):
    """Ozon buyer chat (``/v1/chat/send/message``, ``Client-Id`` + ``Api-Key``)."""

    platform = ChannelType.OZON
    channel_types = (ChannelType.OZON,)
    account_headers = ("x-ozon-client-id",)
    account_payload_keys = ("client_id", "clientId", "seller_id", "shop_id")
    buyer_keys = ("buyer_id", "customer_id", "customerId", "from_id", "sender_id", "user_id")
    thread_keys = ("chat_id", "chatId", "conversation_id") + _CANONICAL_THREAD_KEYS
    api_base = "https://api-seller.ozon.ru"
    api_path = "/v1/chat/send/message"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_ozon_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        client_id = _text_credential(credentials, "client_id")
        api_key = _text_credential(credentials, "api_key")
        missing = [
            name for name, value in (("client_id", client_id), ("api_key", api_key)) if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            headers={
                "content-type": "application/json",
                "client-id": client_id,
                "api-key": api_key,
            },
            body=_json_body({"chat_id": _thread(request), "text": _message_text(request)}),
        )


# ---------------------------------------------------------------------------------
# 7. Walmart Messages
# ---------------------------------------------------------------------------------


class WalmartImAdapter(_BaseMarketplaceImAdapter):
    """Walmart Marketplace Messages (``/v3/messages/send``)."""

    platform = ChannelType.WALMART
    channel_types = (ChannelType.WALMART,)
    account_headers = ("x-walmart-seller-id",)
    account_payload_keys = ("seller_id", "sellerId", "partner_id", "shop_id")
    buyer_keys = (
        "buyer_id",
        "buyerId",
        "customer_id",
        "customerId",
        "sender_id",
        "senderId",
        "from_id",
        "fromId",
    )
    thread_keys = ("conversationId", "threadId", "conversation_id", "chat_id") + (
        _CANONICAL_THREAD_KEYS
    )
    api_base = "https://marketplace.walmartapis.com"
    api_path = "/v3/messages/send"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_walmart_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        access_token = _text_credential(credentials, "access_token")
        seller_id = _text_credential(credentials, "seller_id")
        missing = [
            name
            for name, value in (("access_token", access_token), ("seller_id", seller_id))
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "wm_sec.access_token": access_token,
                "wm_qos.correlation_id": str(uuid4()),
                "wm_svc.name": "Walmart Marketplace",
            },
            body=_json_body(
                {
                    "conversationId": _thread(request),
                    "senderId": seller_id,
                    "receiverId": _recipient(request),
                    "messageType": "text",
                    "messageText": _message_text(request),
                }
            ),
        )


# ---------------------------------------------------------------------------------
# 8. Mercado Libre messages
# ---------------------------------------------------------------------------------


class MercadoLibreImAdapter(_BaseMarketplaceImAdapter):
    """Mercado Libre post-sale messages (``/messages/packs/{pack}/sellers/{seller}``)."""

    platform = ChannelType.MERCADO_LIBRE
    channel_types = (ChannelType.MERCADO_LIBRE,)
    account_headers = ("x-meli-seller-id",)
    account_payload_keys = ("seller_id", "sellerId", "user_id", "site_id")
    buyer_keys = ("buyer_id", "from_user_id", "fromUserId", "sender_id", "senderId")
    thread_keys = ("pack_id", "packId", "conversation_id", "order_id") + _CANONICAL_THREAD_KEYS
    api_base = "https://api.mercadolibre.com"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_mercado_libre_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        access_token = _text_credential(credentials, "access_token")
        seller_id = _text_credential(credentials, "seller_id")
        pack_id = _thread(request)
        missing = [
            name
            for name, value in (
                ("access_token", access_token),
                ("seller_id", seller_id),
                ("pack_id", pack_id),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        path = f"/messages/packs/{_segment(pack_id)}/sellers/{_segment(seller_id)}"
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + path,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {access_token}",
            },
            body=_json_body({"text": _message_text(request), "attachments": []}),
        )


# ---------------------------------------------------------------------------------
# 9. TikTok Shop customer service
# ---------------------------------------------------------------------------------


class TikTokShopImAdapter(_BaseMarketplaceImAdapter):
    """TikTok Shop customer service (``/customer_service/202309/...``)."""

    platform = ChannelType.TIKTOK_SHOP
    channel_types = (ChannelType.TIKTOK_SHOP,)
    account_headers = ("x-tts-shop-domain", "x-tts-shop-id", "x-tts-shop-cipher")
    account_payload_keys = ("shop_id", "shopId", "shop_cipher", "shopCipher", "shop_domain")
    buyer_keys = ("buyer_id", "buyerId", "buyer_user_id", "from_user_id", "sender_id", "user_id")
    thread_keys = ("conversation_id", "conversationId") + _CANONICAL_THREAD_KEYS
    api_base = "https://open-api.tiktokglobalshop.com"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        lowered = _lower_headers(headers)
        app_key = lowered.get("x-tts-app-key", "").strip()
        if app_key and verify_tiktok_webhook_signature(raw_body, headers, app_key, secret):
            return True
        received = (lowered.get("authorization") or "").strip()
        if not received:
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, received.lower())

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        app_key = _text_credential(credentials, "app_key")
        app_secret = _text_credential(credentials, "app_secret")
        access_token = _text_credential(credentials, "access_token")
        conversation_id = _thread(request)
        missing = [
            name
            for name, value in (
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("access_token", access_token),
                ("conversation_id", conversation_id),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        path = (
            f"/customer_service/202309/conversations/{_segment(conversation_id)}/messages"
        )
        body = _json_body(
            {"type": "TEXT", "content": {"text": _message_text(request)}}
        )
        params: dict[str, str] = {"app_key": app_key, "timestamp": str(int(time.time()))}
        shop_cipher = _text_credential(credentials, "shop_cipher")
        if shop_cipher:
            params["shop_cipher"] = shop_cipher
        params["sign"] = sign_tiktok_request(
            app_secret, path, params, body.decode("utf-8")
        )
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + path,
            params=params,
            headers={
                "content-type": "application/json",
                "x-tts-access-token": access_token,
            },
            body=body,
        )


# ---------------------------------------------------------------------------------
# 10. eBay member messages
# ---------------------------------------------------------------------------------


class EbayImAdapter(_BaseMarketplaceImAdapter):
    """eBay member messages (``/commerce/notification/v1/member_message``)."""

    platform = ChannelType.EBAY
    channel_types = (ChannelType.EBAY,)
    account_headers = ("x-ebay-seller-username", "x-ebay-seller-id")
    account_payload_keys = ("seller_username", "seller_id", "sellerId", "recipient_user_id")
    buyer_keys = ("sender_id", "senderId", "buyer_id", "from_user_id", "senderUserId")
    api_base = "https://api.ebay.com"
    api_path = "/commerce/notification/v1/member_message"

    #: The order-feed connector owns eBay's webhook HMAC shape.
    _connector: ClassVar[EbayConnectorAdapter | None] = None

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        connector = EbayImAdapter._connector
        if connector is None:
            connector = EbayConnectorAdapter(
                seller_username="",
                site_id="EBAY_US",
                app_id="",
                cert_id="",
                refresh_token="",
            )
            EbayImAdapter._connector = connector
        return connector.verify_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        access_token = _text_credential(credentials, "access_token")
        if not access_token:
            return _missing(self.platform, ["access_token"])
        payload: dict[str, Any] = {
            "recipientUserId": _recipient(request),
            "messageText": _message_text(request),
        }
        thread = _thread(request)
        if thread:
            payload["referenceId"] = thread
        raw_sandbox = credentials.get("sandbox")
        use_sandbox = (
            raw_sandbox.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(raw_sandbox, str)
            else bool(raw_sandbox)
        )
        default_base_url = EBAY_SANDBOX_BASE_URL if use_sandbox else self.api_base
        return _SendCall(
            method="POST",
            url=_base_url(credentials, default_base_url) + self.api_path,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {access_token}",
            },
            body=_json_body(payload),
        )


# ---------------------------------------------------------------------------------
# 11. Amazon SP-API buyer-seller messaging
# ---------------------------------------------------------------------------------


class AmazonSpImAdapter(_BaseMarketplaceImAdapter):
    """Amazon SP-API buyer-seller messaging (``/messaging/v1/orders/{id}/messages/...``)."""

    platform = ChannelType.AMAZON_SP
    channel_types = (ChannelType.AMAZON_SP,)
    account_headers = ("x-amazon-seller-id",)
    account_payload_keys = ("seller_id", "sellerId", "amazon_seller_id")
    buyer_keys = ("buyer_id", "buyerId", "buyer_email", "sender_id", "customer_id")
    api_base = "https://sellingpartnerapi-na.amazon.com"
    #: Default marketplace for the signed body.
    default_marketplace_id: ClassVar[str] = "ATVPDKIKX0DER"
    #: SP-API messaging action used when the caller does not override it.
    default_action: ClassVar[str] = "confirmCustomizationDetails"

    _connector: ClassVar[AmazonSpApiAdapter | None] = None

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        connector = AmazonSpImAdapter._connector
        if connector is None:
            connector = AmazonSpApiAdapter(
                seller_id="",
                marketplace_id=self.default_marketplace_id,
                lwa_client_id="",
                lwa_client_secret="",
                refresh_token="",
            )
            AmazonSpImAdapter._connector = connector
        return connector.verify_webhook(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        access_token = _text_credential(credentials, "access_token")
        order_id = _order_id_for(request) or _thread(request)
        marketplace_id = (
            _text_credential(credentials, "marketplace_id") or self.default_marketplace_id
        )
        action = _text_credential(request.metadata, "amazon_message_type") or self.default_action
        missing = [
            name
            for name, value in (("access_token", access_token), ("order_id", order_id))
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        path = f"/messaging/v1/orders/{_segment(order_id)}/messages/{_segment(action)}"
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + path,
            headers={
                "content-type": "application/json",
                "x-amz-access-token": access_token,
            },
            body=_json_body(
                {
                    "marketplaceIds": [marketplace_id],
                    "body": _message_text(request),
                }
            ),
        )


# ---------------------------------------------------------------------------------
# 12. Alibaba International (TradeManager IM)
# ---------------------------------------------------------------------------------


class AlibabaIntlImAdapter(_BaseMarketplaceImAdapter):
    """Alibaba International IOP IM (``/alibaba/icbu/im/message/send``)."""

    platform = ChannelType.ALIBABA_EMAIL
    channel_types = (ChannelType.ALIBABA_EMAIL,)
    account_headers = ("x-alibaba-store-id",)
    account_payload_keys = ("store_id", "storeId", "company_id", "supplier_id")
    buyer_keys = ("buyer_id", "buyerId", "buyer_login_id", "sender_id", "from_id", "member_id")
    thread_keys = ("inquiry_id", "inquiryId", "trademanager_id", "session_id") + (
        _CANONICAL_THREAD_KEYS
    )
    conversation_mode = ConversationMode.B2B_INQUIRY
    api_base = "https://openapi-api.alibaba.com"
    api_path = "/alibaba/icbu/im/message/send"

    def _verify(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_alibaba_iop_webhook_signature(raw_body, headers, secret)

    def _prepare_send(
        self, credentials: Mapping[str, Any], request: OutboundMessageRequest
    ) -> _SendCall | _SendFailure:
        app_key = _text_credential(credentials, "app_key")
        app_secret = _text_credential(credentials, "app_secret")
        access_token = _text_credential(credentials, "access_token")
        store_id = _text_credential(credentials, "store_id")
        missing = [
            name
            for name, value in (
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("access_token", access_token),
                ("store_id", store_id),
            )
            if not value
        ]
        if missing:
            return _missing(self.platform, missing)
        params: dict[str, str] = {
            "app_key": app_key,
            "access_token": access_token,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
        }
        params["sign"] = sign_alibaba_iop_request(self.api_path, params, app_secret)
        payload = {
            "store_id": store_id,
            "buyer_id": _recipient(request),
            "session_id": _thread(request),
            "message_type": "text",
            "content": {"text": _message_text(request)},
        }
        return _SendCall(
            method="POST",
            url=_base_url(credentials, self.api_base) + self.api_path,
            params=params,
            headers={"content-type": "application/json"},
            body=_json_body(payload),
        )


# ---------------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------------

_ADAPTER_CLASSES: Final[tuple[type[_BaseMarketplaceImAdapter], ...]] = (
    ShopeeImAdapter,
    LazadaImAdapter,
    TemuImAdapter,
    SheinImAdapter,
    AliExpressImAdapter,
    OzonImAdapter,
    WalmartImAdapter,
    MercadoLibreImAdapter,
    TikTokShopImAdapter,
    EbayImAdapter,
    AmazonSpImAdapter,
    AlibabaIntlImAdapter,
)


def build_marketplace_im_adapters(
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[ChannelType, MessagingChannelAdapter]:
    """Every marketplace-IM adapter, keyed by :class:`ChannelType`."""
    adapters: dict[ChannelType, MessagingChannelAdapter] = {}
    for adapter_class in _ADAPTER_CLASSES:
        adapter = adapter_class(http_transport=http_transport)
        for channel_type in adapter.channel_types:
            adapters[channel_type] = adapter
    return adapters
