"""Sourcing visual-AI processing: SSRF-safe source validation and providers.

Three interchangeable `ImageProcessingProvider` implementations sit behind one
protocol, so the router never learns which vendor rendered an image:

* `SandboxImageProvider` - fully offline. It renders a real, deterministic PNG
  (struct + zlib, no PIL dependency) whose gradient / silhouette bands are a
  pure function of the job parameters, so the same request always produces the
  same bytes and the panel's `<img>` tag renders an actual image.
* `RemoveBgHttpProvider` - remove.bg over HTTPS with `trust_env=False`, an
  injected `httpx.AsyncBaseTransport` for deterministic unit tests and a
  fail-closed API-key guard.
* `OpenAiImageHttpProvider` - OpenAI Images over HTTPS, same guards.

Nothing here imports `erp_core.ai_engine`, `erp_core.orders`, `erp_core.finance`
or `erp_core.channels` (`catalog_isolated` / `domain_modules_never_import_ai_engine`).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import ipaddress
import os
import re
import struct
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.visual_models import SourcingImageJob
from erp_core.catalog.visual_schemas import VisualJobCreate, VisualJobRead
from erp_core.shared.errors import AppError
from erp_core.shared.storage import ObjectStorageService

#: Provider identifier for every supported renderer (mirrors the DB CHECK).
VISUAL_PROVIDERS: tuple[str, ...] = ("sandbox", "remove_bg", "openai_image")
#: Operation identifier for every supported edit (mirrors the DB CHECK).
VISUAL_OPERATIONS: tuple[str, ...] = ("remove_bg", "scene_replace", "translate_overlay")

#: Source images larger than this are refused before any provider is called.
MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024
#: Longest accepted opaque provider error message stored on a failed job.
MAX_ERROR_MESSAGE_CHARS = 500

_ALLOWED_DATA_IMAGE_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "kubernetes.default",
    }
)
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet")
# RFC 6598 carrier-grade NAT space: not ``is_private`` on every CPython release.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
_MEDIA_PATH_RE = re.compile(r"^/media/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[a-z+]+);base64,(?P<payload>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)

DnsResolver = Callable[[str], Awaitable[Sequence[str]]]


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------
def _unsafe_source(detail: str) -> AppError:
    return AppError(
        slug="unsafe-source-image",
        title="Unsafe Source Image URL",
        status_code=422,
        detail=detail,
        code="ERR_UNSAFE_SOURCE_IMAGE",
    )


class VisualProviderError(AppError):
    """A provider could not render the image. The message is never a secret."""

    def __init__(self, *, provider: str, detail: str, code: str = "ERR_VISUAL_PROVIDER_FAILED"):
        super().__init__(
            slug="visual-provider-failed",
            title="Visual Provider Failed",
            status_code=422,
            detail=detail,
            code=code,
        )
        self.provider = provider


# --------------------------------------------------------------------------------------
# Source URL validation (SSRF guard)
# --------------------------------------------------------------------------------------
def sniff_image_mime(data: bytes) -> str | None:
    """Sniff an image MIME from magic bytes only; never trust a caller header."""
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_public_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Fail closed: only globally routable unicast addresses are acceptable."""
    mapped = getattr(value, "ipv4_mapped", None)
    if mapped is not None:
        # ``::ffff:127.0.0.1`` is a loopback target wearing an IPv6 costume.
        return _is_public_ip(mapped)
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
        or (isinstance(value, ipaddress.IPv4Address) and value in _SHARED_ADDRESS_SPACE)
    )


def validate_data_image_url(raw: str) -> str:
    """Accept only `data:image/{png,jpeg,webp,gif};base64,...` with real image bytes.

    `data:image/svg+xml` is refused outright: an SVG data URL is a script
    container, not a raster source image.
    """
    match = _DATA_URL_RE.match(raw)
    if match is None:
        raise _unsafe_source(
            "Inline images must use data:image/png, jpeg, webp or gif;base64,... arithmetic."
        )
    mime = match.group("mime").lower()
    if mime not in _ALLOWED_DATA_IMAGE_MIME:
        raise _unsafe_source(f"Inline image MIME '{mime}' is not allowed.")
    payload = re.sub(r"\s+", "", match.group("payload"))
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _unsafe_source("Inline image payload is not valid base64.") from exc
    if not decoded:
        raise _unsafe_source("Inline image payload is empty.")
    if len(decoded) > MAX_SOURCE_IMAGE_BYTES:
        raise _unsafe_source(
            f"Inline image exceeds the {MAX_SOURCE_IMAGE_BYTES // (1024 * 1024)} MiB limit."
        )
    if sniff_image_mime(decoded) is None:
        raise _unsafe_source("Inline image payload is not a decodable raster image.")
    return raw


async def validate_source_image_url(
    raw_url: str,
    *,
    dns_resolver: DnsResolver | None = None,
) -> str:
    """Allow only https public hosts, safe `data:image/*` payloads or `/media/...` keys.

    Literal private / loopback / link-local / CGNAT targets, single-label
    intranet names and credential-smuggling URLs are refused. When a
    `dns_resolver` is supplied every resolved address must be public, which is
    how the tests prove a DNS name pointing at `169.254.169.254` is rejected.
    """
    candidate = (raw_url or "").strip()
    if not candidate:
        raise _unsafe_source("Source image URL must not be empty.")
    if candidate.lower().startswith("data:"):
        return validate_data_image_url(candidate)
    if candidate.startswith("/"):
        if not _MEDIA_PATH_RE.match(candidate) or ".." in candidate:
            raise _unsafe_source(
                "Internal media paths must look like /media/<path> without traversal segments."
            )
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https":
        raise _unsafe_source(
            f"Scheme '{parsed.scheme or '(none)'}' is not allowed; only https is supported."
        )
    if parsed.username or parsed.password:
        raise _unsafe_source("Source image URL must not embed credentials.")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise _unsafe_source("Source image URL must include a hostname.")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise _unsafe_source(f"Host '{host}' is a private or reserved target.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise _unsafe_source(f"IP literal '{host}' is not a public internet address.")
    elif "." not in host:
        raise _unsafe_source(f"Host '{host}' is not a fully qualified public domain.")

    if dns_resolver is not None:
        resolved = list(await dns_resolver(host))
        if not resolved:
            raise _unsafe_source(f"Host '{host}' did not resolve to any address.")
        for address in resolved:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise _unsafe_source(
                    f"Host '{host}' resolved to an unparsable address."
                ) from exc
            if not _is_public_ip(resolved_ip):
                raise _unsafe_source(
                    f"Host '{host}' resolves to non-public address {resolved_ip}."
                )
    return candidate


# --------------------------------------------------------------------------------------
# Deterministic PNG renderer (no PIL: struct + zlib only)
# --------------------------------------------------------------------------------------
def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _mix(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    ratio: float,
) -> tuple[int, int, int]:
    clamped = max(0.0, min(1.0, ratio))
    return (
        int(round(first[0] + (second[0] - first[0]) * clamped)),
        int(round(first[1] + (second[1] - first[1]) * clamped)),
        int(round(first[2] + (second[2] - first[2]) * clamped)),
    )


def encode_png(width: int, height: int, raw_scanlines: bytes) -> bytes:
    """Wrap unfiltered RGBA scanlines in a spec-valid PNG container."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw_scanlines, 9))
        + _png_chunk(b"IEND", b"")
    )


def render_sandbox_png(*, seed: str, operation: str, size: int = 128) -> bytes:
    """Render a deterministic RGBA PNG whose shape depends on the operation.

    ``remove_bg`` keeps only a centred silhouette (transparent corners),
    ``translate_overlay`` paints horizontal caption bands, and ``scene_replace``
    paints a full-bleed gradient. Two calls with the same arguments are
    byte-identical, which is what makes the job reproducible offline.
    """
    digest = hashlib.sha256(f"{seed}|{operation}".encode()).digest()
    base = (digest[0], digest[1], digest[2])
    accent = (digest[3], digest[4], digest[5])
    centre = (size - 1) / 2
    radius_sq = (size * 0.36) ** 2
    scanlines = bytearray()
    for y in range(size):
        scanlines.append(0)  # PNG filter type 0 (None)
        for x in range(size):
            inside = (x - centre) ** 2 + (y - centre) ** 2 <= radius_sq
            if operation == "remove_bg":
                if not inside:
                    scanlines.extend((0, 0, 0, 0))
                else:
                    scanlines.extend((*_mix(base, accent, y / size), 255))
            elif operation == "translate_overlay":
                band = ((y * 6) // size) % 2
                tone = base if band == 0 else accent
                scanlines.extend((*tone, 255))
            else:
                scanlines.extend((*_mix(base, accent, (x + y) / (2 * size)), 255))
    return encode_png(size, size, bytes(scanlines))


def image_data_url(data: bytes, mime_type: str) -> str:
    """Encode bytes as an inline preview URL a browser renders without object storage."""
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


# --------------------------------------------------------------------------------------
# Provider protocol
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class VisualJobRequest:
    tenant_id: UUID
    job_id: UUID
    provider: str
    operation: str
    source_image_url: str
    target_locale: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class VisualResult:
    mime_type: str
    data: bytes
    provider: str
    note: str | None = None


@runtime_checkable
class ImageProcessingProvider(Protocol):
    """One visual renderer. Implementations must be safe to call concurrently."""

    name: str
    requires_api_key: bool

    async def process(self, job_request: VisualJobRequest) -> VisualResult:
        """Render one job or raise `VisualProviderError` with a non-secret reason."""
        ...


class SandboxImageProvider:
    """Offline renderer: deterministic valid PNG bytes, no network, no API key."""

    name = "sandbox"
    requires_api_key = False

    async def process(self, job_request: VisualJobRequest) -> VisualResult:
        data = render_sandbox_png(
            seed=f"{job_request.source_image_url}|{job_request.prompt or ''}"
            f"|{job_request.target_locale or ''}",
            operation=job_request.operation,
        )
        return VisualResult(
            mime_type="image/png",
            data=data,
            provider=self.name,
            note=f"deterministic sandbox render ({job_request.operation})",
        )


class RemoveBgHttpProvider:
    """remove.bg over HTTPS. Refuses to run without an API key."""

    name = "remove_bg"
    requires_api_key = True
    ENDPOINT = "https://api.remove.bg/v1.0/removebg"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout_seconds

    def _resolve_api_key(self) -> str:
        if self._api_key is not None:
            key = self._api_key.strip()
        else:
            key = os.environ.get("REMOVE_BG_API_KEY", "").strip()
        if not key:
            raise VisualProviderError(
                provider=self.name,
                detail=(
                    "remove.bg is not configured: set REMOVE_BG_API_KEY before requesting "
                    "background removal."
                ),
                code="ERR_VISUAL_PROVIDER_NOT_CONFIGURED",
            )
        return key

    async def process(self, job_request: VisualJobRequest) -> VisualResult:
        api_key = self._resolve_api_key()
        form = {
            "image_url": job_request.source_image_url,
            "size": "preview",
            "type": "product",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.ENDPOINT,
                    headers={"X-Api-Key": api_key, "Accept": "image/png"},
                    data=form,
                )
        except httpx.HTTPError as exc:
            raise VisualProviderError(
                provider=self.name,
                detail=f"remove.bg request failed ({type(exc).__name__}).",
                code="ERR_VISUAL_PROVIDER_UNREACHABLE",
            ) from exc
        if response.status_code != 200:
            raise VisualProviderError(
                provider=self.name,
                detail=f"remove.bg returned HTTP {response.status_code}.",
                code="ERR_VISUAL_PROVIDER_REJECTED",
            )
        mime_type = sniff_image_mime(response.content)
        if mime_type is None:
            raise VisualProviderError(
                provider=self.name,
                detail="remove.bg returned a body that is not a raster image.",
                code="ERR_VISUAL_PROVIDER_INVALID_BODY",
            )
        return VisualResult(mime_type=mime_type, data=response.content, provider=self.name)


class OpenAiImageHttpProvider:
    """OpenAI Images over HTTPS. Refuses to run without an API key."""

    name = "openai_image"
    requires_api_key = True
    ENDPOINT = "https://api.openai.com/v1/images/generations"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 120.0,
        model: str = "gpt-image-1",
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout_seconds
        self._model = model

    def _resolve_api_key(self) -> str:
        if self._api_key is not None:
            key = self._api_key.strip()
        else:
            key = (
                os.environ.get("OPENAI_IMAGE_API_KEY", "").strip()
                or os.environ.get("OPENAI_API_KEY", "").strip()
            )
        if not key:
            raise VisualProviderError(
                provider=self.name,
                detail=(
                    "OpenAI image editing is not configured: set OPENAI_API_KEY before "
                    "requesting a scene re-render."
                ),
                code="ERR_VISUAL_PROVIDER_NOT_CONFIGURED",
            )
        return key

    @staticmethod
    def _build_prompt(job_request: VisualJobRequest) -> str:
        base = (job_request.prompt or "").strip() or "Marketing hero shot of this product."
        locale = (job_request.target_locale or "").strip()
        if job_request.operation == "translate_overlay" and locale:
            return f"{base} Render any embedded caption text in locale '{locale}'."
        if job_request.operation == "remove_bg":
            return f"{base} Cut the product out on a clean transparent background."
        return base

    async def process(self, job_request: VisualJobRequest) -> VisualResult:
        api_key = self._resolve_api_key()
        payload = {
            "model": self._model,
            "prompt": self._build_prompt(job_request),
            "size": "1024x1024",
            "n": 1,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.ENDPOINT,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise VisualProviderError(
                provider=self.name,
                detail=f"OpenAI images request failed ({type(exc).__name__}).",
                code="ERR_VISUAL_PROVIDER_UNREACHABLE",
            ) from exc
        if response.status_code != 200:
            raise VisualProviderError(
                provider=self.name,
                detail=f"OpenAI images returned HTTP {response.status_code}.",
                code="ERR_VISUAL_PROVIDER_REJECTED",
            )
        try:
            body = response.json()
            entry = (body.get("data") or [{}])[0]
            encoded = entry.get("b64_json") or ""
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise VisualProviderError(
                provider=self.name,
                detail="OpenAI images returned no decodable b64_json payload.",
                code="ERR_VISUAL_PROVIDER_INVALID_BODY",
            ) from exc
        mime_type = sniff_image_mime(data)
        if mime_type is None:
            raise VisualProviderError(
                provider=self.name,
                detail="OpenAI images returned a body that is not a raster image.",
                code="ERR_VISUAL_PROVIDER_INVALID_BODY",
            )
        return VisualResult(mime_type=mime_type, data=data, provider=self.name)


def default_visual_providers() -> dict[str, ImageProcessingProvider]:
    """The production provider registry, one entry per DB-allowed provider name."""
    return {
        "sandbox": SandboxImageProvider(),
        "remove_bg": RemoveBgHttpProvider(),
        "openai_image": OpenAiImageHttpProvider(),
    }


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------
class VisualJobService:
    """Create, list, inspect and delete `sourcing_image_jobs` rows."""

    def __init__(self, providers: dict[str, ImageProcessingProvider] | None = None) -> None:
        self._providers = providers if providers is not None else default_visual_providers()

    # -- reads -------------------------------------------------------------------
    async def list_jobs(
        self, session: AsyncSession, tenant_id: UUID, *, limit: int = 50
    ) -> list[VisualJobRead]:
        rows = (
            await session.execute(
                select(SourcingImageJob)
                .where(SourcingImageJob.tenant_id == tenant_id)
                .order_by(SourcingImageJob.created_at.desc(), SourcingImageJob.id.desc())
                .limit(limit)
            )
        ).scalars()
        return [VisualJobRead.model_validate(row) for row in rows]

    async def get_job(
        self, session: AsyncSession, tenant_id: UUID, job_id: UUID
    ) -> VisualJobRead:
        job = await self._load(session, tenant_id, job_id)
        return VisualJobRead.model_validate(job)

    async def get_job_output_bytes(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        job_id: UUID,
        *,
        storage: ObjectStorageService | None = None,
    ) -> tuple[bytes, str]:
        """Return rendered image bytes and MIME type for a completed job."""
        job = await self._load(session, tenant_id, job_id)
        mime_type = job.output_mime_type or "image/png"
        if storage is not None and job.output_r2_key:
            try:
                data = await storage.download_bytes(job.output_r2_key)
                return data, mime_type
            except Exception as exc:  # noqa: BLE001 - map missing/unreachable object cleanly
                raise AppError(
                    slug="visual-output-unavailable",
                    title="Visual Output Unavailable",
                    status_code=404,
                    detail="Rendered image could not be loaded from object storage.",
                    code="ERR_VISUAL_OUTPUT_UNAVAILABLE",
                ) from exc
        if job.output_image_url and job.output_image_url.startswith("data:"):
            match = _DATA_URL_RE.match(job.output_image_url)
            if match is not None:
                parsed_mime = match.group("mime").lower()
                if parsed_mime in _ALLOWED_DATA_IMAGE_MIME:
                    return base64.b64decode(match.group("payload"), validate=True), parsed_mime
        raise AppError(
            slug="visual-output-unavailable",
            title="Visual Output Unavailable",
            status_code=404,
            detail="Visual job has no rendered image output.",
            code="ERR_VISUAL_OUTPUT_UNAVAILABLE",
        )

    async def _load(self, session: AsyncSession, tenant_id: UUID, job_id: UUID) -> SourcingImageJob:
        job = (
            await session.execute(
                select(SourcingImageJob).where(
                    SourcingImageJob.tenant_id == tenant_id,
                    SourcingImageJob.id == job_id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            raise AppError(
                slug="visual-job-not-found",
                title="Visual Job Not Found",
                status_code=404,
                detail="No visual job with that id exists in this workspace.",
                code="ERR_VISUAL_JOB_NOT_FOUND",
            )
        return job

    # -- writes ------------------------------------------------------------------
    async def create_job(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        created_by: UUID,
        payload: VisualJobCreate,
        *,
        storage: ObjectStorageService | None = None,
    ) -> VisualJobRead:
        """Validate the source, run the provider and persist the terminal job state.

        A provider failure is a *recorded outcome*, not an HTTP error: the row is
        persisted as `failed` with a non-secret `error_message` so the operator
        sees exactly why (e.g. "remove.bg is not configured").
        """
        source = await validate_source_image_url(payload.source_image_url)
        job_id = uuid4()
        job = SourcingImageJob(
            id=job_id,
            tenant_id=tenant_id,
            variant_id=payload.variant_id,
            provider=payload.provider,
            operation=payload.operation,
            source_image_url=source,
            target_locale=payload.target_locale,
            prompt=payload.prompt,
            status="processing",
            created_by=created_by,
        )
        session.add(job)
        await session.flush()

        request = VisualJobRequest(
            tenant_id=tenant_id,
            job_id=job_id,
            provider=payload.provider,
            operation=payload.operation,
            source_image_url=source,
            target_locale=payload.target_locale,
            prompt=payload.prompt,
        )
        provider = self._providers[payload.provider]
        completed_at = datetime.now(UTC)
        try:
            result = await provider.process(request)
        except VisualProviderError as exc:
            job.status = "failed"
            job.error_message = exc.detail[:MAX_ERROR_MESSAGE_CHARS]
            job.completed_at = completed_at
        except Exception as exc:  # noqa: BLE001 - provider internals must never leak
            job.status = "failed"
            job.error_message = (
                f"{payload.provider} rendering failed ({type(exc).__name__})."
            )[:MAX_ERROR_MESSAGE_CHARS]
            job.completed_at = completed_at
        else:
            url, r2_key = await self._persist_output(storage, tenant_id, job_id, result)
            job.status = "completed"
            job.output_image_url = url
            job.output_mime_type = result.mime_type
            job.output_r2_key = r2_key
            job.completed_at = completed_at

        await session.flush()
        await session.refresh(job)
        return VisualJobRead.model_validate(job)

    async def delete_job(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        job_id: UUID,
        *,
        storage: ObjectStorageService | None = None,
    ) -> None:
        job = await self._load(session, tenant_id, job_id)
        r2_key = job.output_r2_key
        await session.delete(job)
        await session.flush()
        if storage is not None and r2_key:
            with contextlib.suppress(Exception):
                # A stale R2 object must never fail the row delete.
                await storage.delete_prefix(r2_key)

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _is_browser_routable_url(url: str) -> bool:
        """Return True when a presigned S3/R2 URL is directly reachable by browsers."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if parsed.scheme != "https" or not host or "." not in host:
            return False
        return host not in {"127.0.0.1", "0.0.0.0", "localhost"} and not host.endswith(
            (".local", ".internal")
        )

    @classmethod
    async def _persist_output(
        cls,
        storage: ObjectStorageService | None,
        tenant_id: UUID,
        job_id: UUID,
        result: VisualResult,
    ) -> tuple[str, str | None]:
        """Store in R2/MinIO when attached, otherwise return an inline PNG preview.

        When the configured object-storage endpoint uses an internal Docker/LAN
        hostname (for example `http://minio:9000`), browsers on the host cannot
        resolve it directly; emit the authenticated BFF stream path instead.
        """
        extension = _MIME_EXT.get(result.mime_type, "png")
        r2_key = f"visual-jobs/{tenant_id}/{job_id}.{extension}"
        if storage is not None:
            try:
                await storage.upload_bytes(r2_key, result.data, content_type=result.mime_type)
                presigned = await storage.generate_presigned_get_url(r2_key)
                if cls._is_browser_routable_url(presigned):
                    return presigned, r2_key
                return f"/api/bff/v1/catalog/visual-jobs/{job_id}/output", r2_key
            except Exception:  # noqa: BLE001 - offline dev / MinIO down must still render
                return image_data_url(result.data, result.mime_type), None
        return image_data_url(result.data, result.mime_type), None
