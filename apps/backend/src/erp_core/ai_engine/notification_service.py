"""Outbound notifications: Feishu / DingTalk / WeCom / generic webhook delivery.

Design contract:

* **SSRF-safe by construction.** Channel URLs are validated with the exact rules that
  guard product sourcing (`erp_core.catalog.sourcing_service.validate_source_url`), and
  the hostname is re-resolved immediately before every POST
  (`resolve_and_validate_public_url`) so a DNS-rebinding answer cannot turn a public host
  into `127.0.0.1`. Local targets are only reachable when an operator opts in with
  `ALLOW_LOCAL_WEBHOOK_URLS=1` or a test injects `allow_local_urls=True`.
* **Secrets are never returned.** The bot signing secret is stored through
  `EnvelopeCryptoService` with `AAD = f"{tenant_id}:{channel_id}:webhook_signing_secret"`
  and the read models expose only `has_signing_secret` plus a masked URL preview.
* **Honest delivery ledger.** A row is `pending` until a real HTTP attempt happens; a
  failure becomes `retrying` with a bounded 10-minute backoff (three attempts) and then
  `failed`. Nothing is reported as `sent` without a 2xx response.
* **Deterministic transport.** An `httpx.AsyncClient` or an `AsyncBaseTransport` can be
  injected, so unit tests never touch the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse
from uuid import UUID, uuid4

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.notification_models import NotificationChannel, NotificationDelivery
from erp_core.ai_engine.notification_schemas import (
    DeliveryRetrySweepResult,
    NotificationChannelCreate,
    NotificationChannelRead,
    NotificationChannelUpdate,
    NotificationDeliveryRead,
    NotificationTestResult,
)
from erp_core.catalog.sourcing_service import (
    DnsResolver,
    resolve_and_validate_public_url,
    validate_source_url,
)
from erp_core.shared.config import Settings, get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import record_not_found
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

#: Credential-type discriminator bound into the envelope AAD for the bot signing secret.
WEBHOOK_SECRET_CRED_TYPE = "webhook_signing_secret"
#: Operator escape hatch for self-hosted bots on a private network / local tests.
ALLOW_LOCAL_WEBHOOK_URLS_ENV = "ALLOW_LOCAL_WEBHOOK_URLS"
#: Lower rank == more severe. A channel admits every priority ranked `<=` its minimum.
PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}
MAX_DELIVERY_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(minutes=10)
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_PROVIDERS = frozenset({"feishu", "dingtalk", "wecom", "generic_webhook"})


def _error(status_code: int, code: str, detail: str) -> AppError:
    return AppError(
        slug="notification-request-rejected",
        title="Notification Request Rejected",
        status_code=status_code,
        detail=detail,
        code=code,
    )


def allow_local_webhook_urls() -> bool:
    """Read the explicit operator opt-in for private/local webhook targets."""
    return os.environ.get(ALLOW_LOCAL_WEBHOOK_URLS_ENV, "").strip().lower() in _TRUTHY


def channel_accepts_priority(min_priority: str, priority: str) -> bool:
    """Priority filter: `P0` channel gets P0 only, `P1` gets P0+P1, `P2` gets all three."""
    if priority not in PRIORITY_RANK:
        raise _error(422, "ERR_UNKNOWN_NOTIFICATION_PRIORITY", f"Unknown priority '{priority}'.")
    minimum = PRIORITY_RANK.get(min_priority)
    if minimum is None:
        raise _error(
            422,
            "ERR_UNKNOWN_NOTIFICATION_PRIORITY",
            f"Unknown channel minimum priority '{min_priority}'.",
        )
    return PRIORITY_RANK[priority] <= minimum


def mask_webhook_url(raw: str) -> str:
    """Masked preview: scheme + host are safe, the token-bearing path is redacted.

    Feishu/DingTalk/WeCom bot URLs carry the bot access token in the path, so the path is
    reduced to a four-character tail that lets an operator tell two bots apart without
    leaking a usable secret.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    host = parsed.hostname or ""
    if not host:
        return "***"
    path = parsed.path.rstrip("/")
    token = path.rsplit("/", 1)[-1] if path else ""
    tail = token[-4:] if len(token) >= 4 else ""
    suffix = f"***{tail}" if tail else "***"
    return f"{parsed.scheme}://{host}/{suffix}"


def validate_webhook_url(raw: str, *, allow_local: bool | None = None) -> str:
    """Validate a channel URL with the sourcing SSRF rules (or a local-only exception)."""
    candidate = (raw or "").strip()
    if not candidate:
        raise _error(422, "ERR_WEBHOOK_URL_REQUIRED", "Webhook URL must not be empty.")
    local_allowed = allow_local_webhook_urls() if allow_local is None else allow_local
    parsed = urlparse(candidate)
    if parsed.username or parsed.password:
        raise _error(
            422,
            "ERR_WEBHOOK_URL_CREDENTIALS",
            "Webhook URL must not embed credentials.",
        )
    if local_allowed:
        if parsed.scheme.lower() not in {"http", "https"} or not (parsed.hostname or "").strip():
            raise _error(
                422,
                "ERR_UNSAFE_WEBHOOK_URL",
                "Webhook URL must be an http(s) URL with a hostname.",
            )
        return candidate
    try:
        return validate_source_url(candidate)
    except AppError as exc:
        raise _error(
            422,
            "ERR_UNSAFE_WEBHOOK_URL",
            f"Webhook URL rejected by the SSRF guard: {exc.detail}",
        ) from exc


async def ensure_public_webhook_target(
    url: str,
    *,
    allow_local: bool,
    dns_resolver: DnsResolver | None = None,
) -> str:
    """Re-validate the URL and every address the host resolves to, just before sending."""
    if allow_local:
        return validate_webhook_url(url, allow_local=True)
    try:
        resolved_url, _addresses = await resolve_and_validate_public_url(
            url, dns_resolver=dns_resolver
        )
    except AppError as exc:
        raise _error(
            422,
            "ERR_UNSAFE_WEBHOOK_URL",
            f"Webhook target rejected by the SSRF guard: {exc.detail}",
        ) from exc
    return resolved_url


# --------------------------------------------------------------------------------------
# Provider signing and payload shaping (pure functions, unit-testable without a server)
# --------------------------------------------------------------------------------------


def feishu_signature(secret: str, timestamp: str) -> str:
    """Feishu bot custom signature: base64(HMAC-SHA256(key=f"{ts}\\n{secret}", msg=b""))."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def dingtalk_signature(secret: str, timestamp_ms: int) -> str:
    """DingTalk bot signature: quote_plus(base64(HMAC-SHA256(secret, f"{ts}\\n{secret}")))."""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return quote_plus(base64.b64encode(digest).decode("ascii"))


def generic_webhook_signature(secret: str, body: bytes) -> str:
    """Generic webhook signature: hex HMAC-SHA256 of the exact JSON bytes we POST."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def serialize_webhook_payload(payload: dict[str, Any]) -> bytes:
    """Canonical serialization, so the generic-Webhook signature always matches the body."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    """The exact HTTP call a channel produces, kept inspectable for tests."""

    url: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        return serialize_webhook_payload(self.payload)


def _markdown_text(title: str, body: str, deep_link: str | None) -> str:
    rendered = f"**{title}**\n\n{body}"
    if deep_link:
        rendered = f"{rendered}\n\n[打开工作台 / Open in workbench]({deep_link})"
    return rendered


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_webhook_request(
    *,
    provider: str,
    webhook_url: str,
    title: str,
    body: str,
    deep_link: str | None = None,
    secret: str | None = None,
    event_type: str = "notification",
    priority: str = "P1",
    now: datetime | None = None,
) -> WebhookRequest:
    """Shape and sign the provider-specific request. The URL is already SSRF-validated."""
    if provider not in _PROVIDERS:
        raise _error(422, "ERR_UNKNOWN_NOTIFICATION_PROVIDER", f"Unknown provider '{provider}'.")
    moment = now or datetime.now(UTC)
    text = _markdown_text(title, body, deep_link)
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if provider == "feishu":
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if secret:
            timestamp = str(int(moment.timestamp()))
            payload["timestamp"] = timestamp
            payload["sign"] = feishu_signature(secret, timestamp)
        return WebhookRequest(url=webhook_url, payload=payload, headers=headers)

    if provider == "dingtalk":
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        target = webhook_url
        if secret:
            timestamp_ms = int(moment.timestamp() * 1000)
            target = _append_query(
                webhook_url,
                {
                    "timestamp": str(timestamp_ms),
                    "sign": dingtalk_signature(secret, timestamp_ms),
                },
            )
        return WebhookRequest(url=target, payload=payload, headers=headers)

    if provider == "wecom":
        payload = {"msgtype": "markdown", "markdown": {"content": text}}
        return WebhookRequest(url=webhook_url, payload=payload, headers=headers)

    payload = {
        "event_type": event_type,
        "priority": priority,
        "title": title,
        "body": body,
        "deep_link": deep_link,
    }
    request = WebhookRequest(url=webhook_url, payload=payload, headers=headers)
    if secret:
        signed_headers = dict(headers)
        signed_headers["X-ERP-Signature"] = generic_webhook_signature(secret, request.body)
        return WebhookRequest(url=webhook_url, payload=payload, headers=signed_headers)
    return request


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


class NotificationService:
    """Tenant-scoped channel CRUD, priority-filtered queuing and the real HTTP dispatch."""

    def __init__(
        self,
        *,
        crypto: EnvelopeCryptoService | None = None,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_local_urls: bool | None = None,
        dns_resolver: DnsResolver | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._crypto = crypto
        self._client = client
        self._transport = transport
        self._allow_local_urls = allow_local_urls
        self._dns_resolver = dns_resolver
        self._timeout_seconds = timeout_seconds

    # -- infrastructure -----------------------------------------------------

    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def crypto(self) -> EnvelopeCryptoService:
        if self._crypto is None:
            self._crypto = EnvelopeCryptoService(self.settings())
        return self._crypto

    def local_targets_allowed(self) -> bool:
        return (
            allow_local_webhook_urls()
            if self._allow_local_urls is None
            else self._allow_local_urls
        )

    async def _post(self, request: WebhookRequest) -> tuple[int | None, str | None]:
        """POST the signed payload; returns (http_status, error) and never raises on transport."""
        body = request.body
        try:
            if self._client is not None:
                response = await self._client.post(
                    request.url, content=body, headers=request.headers
                )
            elif self._transport is not None:
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=self._timeout_seconds
                ) as client:
                    response = await client.post(
                        request.url, content=body, headers=request.headers
                    )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(
                        request.url, content=body, headers=request.headers
                    )
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if 200 <= response.status_code < 300:
            return response.status_code, None
        return response.status_code, f"HTTP {response.status_code}: {response.text[:200]}"

    # -- secret envelope ----------------------------------------------------

    async def _encrypt_secret(
        self, session: AsyncSession, tenant_id: UUID, channel_id: UUID, secret: str
    ) -> dict[str, Any]:
        ciphertext, nonce, auth_tag, key_version = await self.crypto().encrypt_secret(
            session, tenant_id, channel_id, WEBHOOK_SECRET_CRED_TYPE, secret
        )
        return {
            "secret_ciphertext": ciphertext,
            "secret_nonce": nonce,
            "secret_auth_tag": auth_tag,
            "secret_key_version": key_version,
        }

    async def _decrypt_secret(
        self, session: AsyncSession, channel: NotificationChannel
    ) -> str | None:
        if (
            channel.secret_ciphertext is None
            or channel.secret_nonce is None
            or channel.secret_auth_tag is None
            or channel.secret_key_version is None
        ):
            return None
        return await self.crypto().decrypt_secret(
            session,
            channel.tenant_id,
            channel.id,
            WEBHOOK_SECRET_CRED_TYPE,
            bytes(channel.secret_ciphertext),
            bytes(channel.secret_nonce),
            bytes(channel.secret_auth_tag),
            int(channel.secret_key_version),
        )

    # -- read models --------------------------------------------------------

    @staticmethod
    def channel_read(channel: NotificationChannel) -> NotificationChannelRead:
        return NotificationChannelRead(
            id=channel.id,
            name=channel.name,
            provider=channel.provider,
            webhook_url_preview=mask_webhook_url(channel.webhook_url),
            has_signing_secret=channel.secret_ciphertext is not None,
            min_priority=channel.min_priority,
            is_enabled=channel.is_enabled,
            last_tested_at=channel.last_tested_at,
            last_error=channel.last_error,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    @staticmethod
    def _delivery_read(
        delivery: NotificationDelivery, channel: NotificationChannel
    ) -> NotificationDeliveryRead:
        return NotificationDeliveryRead(
            id=delivery.id,
            channel_id=delivery.channel_id,
            channel_name=channel.name,
            provider=channel.provider,
            priority=delivery.priority,
            event_type=delivery.event_type,
            title=delivery.title,
            body=delivery.body,
            deep_link=delivery.deep_link,
            status=delivery.status,
            attempts=delivery.attempts,
            next_retry_at=delivery.next_retry_at,
            http_status=delivery.http_status,
            error_message=delivery.error_message,
            created_at=delivery.created_at,
            sent_at=delivery.sent_at,
        )

    # -- channel CRUD -------------------------------------------------------

    async def list_channels(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[NotificationChannelRead]:
        channels = (
            await session.scalars(
                select(NotificationChannel)
                .where(NotificationChannel.tenant_id == tenant_id)
                .order_by(NotificationChannel.created_at, NotificationChannel.name)
            )
        ).all()
        return [self.channel_read(channel) for channel in channels]

    async def _load_channel(
        self, session: AsyncSession, tenant_id: UUID, channel_id: UUID
    ) -> NotificationChannel:
        channel = await session.scalar(
            select(NotificationChannel).where(
                NotificationChannel.tenant_id == tenant_id,
                NotificationChannel.id == channel_id,
            )
        )
        if channel is None:
            raise record_not_found("Notification channel")
        return channel

    async def create_channel(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: NotificationChannelCreate,
        *,
        actor_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> NotificationChannelRead:
        webhook_url = validate_webhook_url(
            payload.webhook_url, allow_local=self.local_targets_allowed()
        )
        channel_id = uuid4()
        secret_columns: dict[str, Any] = {}
        if payload.secret:
            secret_columns = await self._encrypt_secret(
                session, tenant_id, channel_id, payload.secret
            )
        channel = NotificationChannel(
            id=channel_id,
            tenant_id=tenant_id,
            name=payload.name.strip(),
            provider=payload.provider,
            webhook_url=webhook_url,
            min_priority=payload.min_priority,
            is_enabled=payload.is_enabled,
            **secret_columns,
        )
        session.add(channel)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.notification.channel.created",
            "notification_channel",
            channel.id,
            after_state={
                "name": channel.name,
                "provider": channel.provider,
                "min_priority": channel.min_priority,
                "has_signing_secret": channel.secret_ciphertext is not None,
                "webhook_url_preview": mask_webhook_url(channel.webhook_url),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return self.channel_read(channel)

    async def update_channel(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_id: UUID,
        payload: NotificationChannelUpdate,
        *,
        actor_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> NotificationChannelRead:
        channel = await self._load_channel(session, tenant_id, channel_id)
        before = {
            "name": channel.name,
            "min_priority": channel.min_priority,
            "is_enabled": channel.is_enabled,
        }
        if payload.name is not None:
            channel.name = payload.name.strip()
        if payload.webhook_url is not None:
            channel.webhook_url = validate_webhook_url(
                payload.webhook_url, allow_local=self.local_targets_allowed()
            )
        if payload.min_priority is not None:
            channel.min_priority = payload.min_priority
        if payload.is_enabled is not None:
            channel.is_enabled = payload.is_enabled
        if payload.secret is not None:
            stripped = payload.secret.strip()
            if stripped:
                for column, value in (
                    await self._encrypt_secret(session, tenant_id, channel.id, stripped)
                ).items():
                    setattr(channel, column, value)
            else:
                channel.secret_ciphertext = None
                channel.secret_nonce = None
                channel.secret_auth_tag = None
                channel.secret_key_version = None
        channel.updated_at = datetime.now(UTC)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.notification.channel.updated",
            "notification_channel",
            channel.id,
            before_state=before,
            after_state={
                "name": channel.name,
                "min_priority": channel.min_priority,
                "is_enabled": channel.is_enabled,
                "has_signing_secret": channel.secret_ciphertext is not None,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return self.channel_read(channel)

    async def delete_channel(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_id: UUID,
        *,
        actor_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        channel = await self._load_channel(session, tenant_id, channel_id)
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.notification.channel.deleted",
            "notification_channel",
            channel.id,
            before_state={
                "name": channel.name,
                "provider": channel.provider,
                "min_priority": channel.min_priority,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await session.delete(channel)
        await session.flush()

    # -- queuing ------------------------------------------------------------

    async def queue_delivery(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        event_type: str,
        title: str,
        body: str,
        priority: str,
        deep_link: str | None = None,
    ) -> list[NotificationDelivery]:
        """Insert one pending delivery per enabled channel that admits this priority."""
        if priority not in PRIORITY_RANK:
            raise _error(
                422, "ERR_UNKNOWN_NOTIFICATION_PRIORITY", f"Unknown priority '{priority}'."
            )
        channels = (
            await session.scalars(
                select(NotificationChannel)
                .where(
                    NotificationChannel.tenant_id == tenant_id,
                    NotificationChannel.is_enabled.is_(True),
                )
                .order_by(NotificationChannel.created_at)
            )
        ).all()
        deliveries: list[NotificationDelivery] = []
        for channel in channels:
            if not channel_accepts_priority(channel.min_priority, priority):
                continue
            delivery = NotificationDelivery(
                tenant_id=tenant_id,
                channel_id=channel.id,
                priority=priority,
                event_type=event_type,
                title=title,
                body=body,
                deep_link=deep_link,
                status="pending",
                attempts=0,
            )
            session.add(delivery)
            deliveries.append(delivery)
        if deliveries:
            await session.flush()
        return deliveries

    # -- dispatch -----------------------------------------------------------

    async def _record_failure(
        self,
        delivery: NotificationDelivery,
        *,
        http_status: int | None,
        error_message: str,
        retryable: bool,
    ) -> NotificationDelivery:
        now = datetime.now(UTC)
        attempts = delivery.attempts + 1
        delivery.attempts = attempts
        delivery.http_status = http_status
        delivery.error_message = error_message
        if retryable and attempts < MAX_DELIVERY_ATTEMPTS:
            delivery.status = "retrying"
            delivery.next_retry_at = now + RETRY_BACKOFF
        else:
            delivery.status = "failed"
            delivery.next_retry_at = None
        return delivery

    async def dispatch_delivery(
        self,
        session: AsyncSession,
        delivery: NotificationDelivery,
        *,
        channel: NotificationChannel | None = None,
    ) -> NotificationDelivery:
        """Attempt one real HTTP delivery and persist the honest outcome."""
        target_channel = channel or await session.scalar(
            select(NotificationChannel).where(
                NotificationChannel.tenant_id == delivery.tenant_id,
                NotificationChannel.id == delivery.channel_id,
            )
        )
        if target_channel is None:
            await self._record_failure(
                delivery,
                http_status=None,
                error_message="Notification channel no longer exists.",
                retryable=False,
            )
            await session.flush()
            return delivery

        try:
            secret = await self._decrypt_secret(session, target_channel)
        except AppError as exc:
            delivery.attempts += 1
            delivery.status = "failed"
            delivery.next_retry_at = None
            delivery.error_message = f"Secret decryption failed: {exc.detail}"
            await session.flush()
            return delivery

        request = build_webhook_request(
            provider=target_channel.provider,
            webhook_url=target_channel.webhook_url,
            title=delivery.title,
            body=delivery.body,
            deep_link=delivery.deep_link,
            secret=secret,
            event_type=delivery.event_type,
            priority=delivery.priority,
        )
        try:
            safe_url = await ensure_public_webhook_target(
                request.url,
                allow_local=self.local_targets_allowed(),
                dns_resolver=self._dns_resolver,
            )
        except AppError as exc:
            # A non-public target never becomes public by retrying.
            await self._record_failure(
                delivery,
                http_status=None,
                error_message=exc.detail,
                retryable=False,
            )
            await session.flush()
            return delivery

        http_status, error_message = await self._post(
            WebhookRequest(url=safe_url, payload=request.payload, headers=request.headers)
        )
        if error_message is None and http_status is not None:
            delivery.status = "sent"
            delivery.attempts += 1
            delivery.http_status = http_status
            delivery.error_message = None
            delivery.next_retry_at = None
            delivery.sent_at = datetime.now(UTC)
        else:
            await self._record_failure(
                delivery,
                http_status=http_status,
                error_message=error_message or "Delivery failed without a transport error.",
                retryable=True,
            )
        await session.flush()
        return delivery

    async def retry_due_deliveries(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int = 50,
    ) -> DeliveryRetrySweepResult:
        """Dispatch every `retrying` row whose backoff elapsed, up to the attempt ceiling."""
        due = (
            await session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.tenant_id == tenant_id,
                    NotificationDelivery.status == "retrying",
                    NotificationDelivery.next_retry_at.is_not(None),
                    NotificationDelivery.next_retry_at <= func.now(),
                    NotificationDelivery.attempts < MAX_DELIVERY_ATTEMPTS,
                )
                .order_by(NotificationDelivery.next_retry_at)
                .limit(limit)
            )
        ).all()
        sent = retrying = failed = 0
        for delivery in due:
            await self.dispatch_delivery(session, delivery)
            if delivery.status == "sent":
                sent += 1
            elif delivery.status == "retrying":
                retrying += 1
            else:
                failed += 1
        return DeliveryRetrySweepResult(
            attempted=len(due), sent=sent, retrying=retrying, failed=failed
        )

    async def test_channel(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_id: UUID,
        *,
        actor_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> NotificationTestResult:
        """Send a real test ping through the channel and record its outcome."""
        channel = await self._load_channel(session, tenant_id, channel_id)
        now = datetime.now(UTC)
        secret = await self._decrypt_secret(session, channel)
        request = build_webhook_request(
            provider=channel.provider,
            webhook_url=channel.webhook_url,
            title="OmniERP 通知渠道测试 / Notification channel test",
            body=(
                f"渠道 {channel.name} 已连接到 OmniERP 通知中心，此消息由人工测试触发。\n"
                f"Channel {channel.name} is wired into the OmniERP notification center; "
                "this message was triggered by a manual test."
            ),
            secret=secret,
            event_type="channel.test",
            priority=channel.min_priority,
            now=now,
        )
        error_message: str | None = None
        http_status: int | None = None
        try:
            safe_url = await ensure_public_webhook_target(
                request.url,
                allow_local=self.local_targets_allowed(),
                dns_resolver=self._dns_resolver,
            )
        except AppError as exc:
            error_message = exc.detail
        else:
            http_status, error_message = await self._post(
                WebhookRequest(url=safe_url, payload=request.payload, headers=request.headers)
            )
        channel.last_tested_at = now
        channel.last_error = error_message
        channel.updated_at = now
        await session.flush()
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.notification.channel.tested",
            "notification_channel",
            channel.id,
            after_state={
                "ok": error_message is None,
                "http_status": http_status,
                "error_message": error_message,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return NotificationTestResult(
            channel_id=channel.id,
            ok=error_message is None,
            http_status=http_status,
            error_message=error_message,
            tested_at=now,
        )

    # -- delivery reads -----------------------------------------------------

    async def list_deliveries(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        channel_id: UUID | None = None,
    ) -> PaginatedResponse[NotificationDeliveryRead]:
        conditions = [NotificationDelivery.tenant_id == tenant_id]
        if status is not None:
            conditions.append(NotificationDelivery.status == status)
        if channel_id is not None:
            conditions.append(NotificationDelivery.channel_id == channel_id)
        total = int(
            await session.scalar(
                select(func.count())
                .select_from(NotificationDelivery)
                .where(*conditions)
            )
            or 0
        )
        rows = (
            await session.execute(
                select(NotificationDelivery, NotificationChannel)
                .join(
                    NotificationChannel,
                    (NotificationChannel.id == NotificationDelivery.channel_id)
                    & (NotificationChannel.tenant_id == NotificationDelivery.tenant_id),
                )
                .where(*conditions)
                .order_by(NotificationDelivery.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        items = [self._delivery_read(delivery, channel) for delivery, channel in rows]
        return PaginatedResponse.build(items=items, total=total, limit=limit, offset=offset)

    async def pending_count(self, session: AsyncSession, tenant_id: UUID) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(NotificationDelivery)
                .where(
                    NotificationDelivery.tenant_id == tenant_id,
                    NotificationDelivery.status.in_(("pending", "retrying")),
                )
            )
            or 0
        )
