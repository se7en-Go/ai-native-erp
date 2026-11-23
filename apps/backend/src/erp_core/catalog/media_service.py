"""Unified S3/MinIO media storage & SSRF-safe external image mirroring for catalog & sourcing."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import ProductMediaAsset
from erp_core.shared.config import get_settings
from erp_core.shared.errors import AppError
from erp_core.shared.storage import ObjectStorageService

MAX_MEDIA_BYTES = 10 * 1024 * 1024
_IMAGE_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
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
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

MediaDnsResolver = Callable[[str], Awaitable[Sequence[str]]]


def detect_image_mime(data: bytes) -> str | None:
    """Sniff image MIME purely from magic bytes; never trust caller headers."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public_ip(mapped)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_ADDRESS_SPACE)
    )


async def _default_dns_resolver(host: str) -> Sequence[str]:
    try:
        infos = await _to_thread_getaddrinfo(host)
    except OSError:
        return ()
    return tuple({str(info[4][0]) for info in infos})


async def _to_thread_getaddrinfo(host: str) -> list[Any]:
    import asyncio

    return await asyncio.to_thread(socket.getaddrinfo, host, None)


async def validate_public_https_image_url(
    raw_url: str,
    *,
    dns_resolver: MediaDnsResolver | None = None,
    allow_http: bool = False,
) -> str:
    """Reject internal/private/loopback targets before fetching external images."""
    candidate = (raw_url or "").strip()
    parsed = urlparse(candidate)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc:
        raise AppError(
            slug="invalid-media-url",
            status_code=422,
            code="ERR_MEDIA_URL_UNSAFE",
            title="Unsafe Media URL",
            detail="Only public https:// image URLs may be mirrored.",
        )
    if parsed.username or parsed.password:
        raise AppError(
            slug="invalid-media-url",
            status_code=422,
            code="ERR_MEDIA_URL_UNSAFE",
            title="Unsafe Media URL",
            detail="Embedded credentials in image URLs are refused.",
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise AppError(
            slug="unsafe-media-host",
            status_code=422,
            code="ERR_MEDIA_URL_UNSAFE",
            title="Unsafe Media URL",
            detail=f"Host '{host or raw_url}' is blocked by SSRF policy.",
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise AppError(
                slug="unsafe-media-ip",
                status_code=422,
                code="ERR_MEDIA_URL_UNSAFE",
                title="Unsafe Media URL",
                detail=f"IP '{host}' is not a public internet address.",
            )
        return candidate

    resolver = dns_resolver or _default_dns_resolver
    answers = await resolver(host)
    if not answers:
        raise AppError(
            slug="unresolvable-media-host",
            status_code=422,
            code="ERR_MEDIA_URL_UNSAFE",
            title="Unsafe Media URL",
            detail=f"Image host '{host}' did not resolve to any address.",
        )
    for answer in answers:
        resolved = ipaddress.ip_address(str(answer).split("%", 1)[0])
        if not _is_public_ip(resolved):
            raise AppError(
                slug="unsafe-media-resolved-ip",
                status_code=422,
                code="ERR_MEDIA_URL_UNSAFE",
                title="Unsafe Media URL",
                detail=f"Image host '{host}' resolves to a non-public address.",
            )
    return candidate


class MediaAssetRead(BaseModel):
    id: UUID
    tenant_id: UUID
    product_id: UUID | None = None
    variant_id: UUID | None = None
    sourcing_draft_id: UUID | None = None
    storage_key: str
    source_url: str | None = None
    sha256: str
    mime_type: str
    byte_size: int
    sort_order: int = 0
    alt_text: str = ""
    content_path: str
    presigned_url: str | None = None
    created_at: datetime


class MediaUrlIngestRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=12)
    product_id: UUID | None = None
    variant_id: UUID | None = None
    sourcing_draft_id: UUID | None = None
    alt_text: str = Field(default="", max_length=255)


class CatalogMediaService:
    """Stores verified image assets in MinIO/R2 and metadata in PostgreSQL (FORCE RLS)."""

    def __init__(self, storage: ObjectStorageService | None = None) -> None:
        self._storage = storage

    def _get_storage(self) -> ObjectStorageService:
        if self._storage is None:
            self._storage = ObjectStorageService(get_settings())
        return self._storage

    @staticmethod
    def _to_read(asset: ProductMediaAsset, *, presigned_url: str | None = None) -> MediaAssetRead:
        return MediaAssetRead(
            id=asset.id,
            tenant_id=asset.tenant_id,
            product_id=asset.product_id,
            variant_id=asset.variant_id,
            sourcing_draft_id=asset.sourcing_draft_id,
            storage_key=asset.storage_key,
            source_url=asset.source_url,
            sha256=asset.sha256,
            mime_type=asset.mime_type,
            byte_size=asset.byte_size,
            sort_order=asset.sort_order,
            alt_text=asset.alt_text,
            content_path=f"/api/v1/catalog/media/{asset.id}/content",
            presigned_url=presigned_url,
            created_at=asset.created_at,
        )

    async def upload_image_bytes(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        filename: str,
        data: bytes,
        product_id: UUID | None = None,
        variant_id: UUID | None = None,
        sourcing_draft_id: UUID | None = None,
        alt_text: str = "",
        sort_order: int = 0,
        source_url: str | None = None,
    ) -> MediaAssetRead:
        if not data or len(data) > MAX_MEDIA_BYTES:
            raise AppError(
                slug="media-size-invalid",
                status_code=422,
                code="ERR_MEDIA_SIZE_INVALID",
                title="Invalid Media Size",
                detail=f"Image payload must be between 1 byte and {MAX_MEDIA_BYTES} bytes.",
            )
        mime_type = detect_image_mime(data)
        if mime_type is None:
            raise AppError(
                slug="media-type-unsupported",
                status_code=422,
                code="ERR_MEDIA_TYPE_UNSUPPORTED",
                title="Unsupported Image Format",
                detail="Only JPEG, PNG, WEBP, and GIF image files are supported.",
            )
        ext = _IMAGE_EXT_BY_MIME[mime_type]
        digest = hashlib.sha256(data).hexdigest()
        asset_id = uuid4()
        storage_key = f"tenants/{tenant_id}/media/{digest[:16]}-{asset_id.hex[:8]}.{ext}"
        storage = self._get_storage()
        await storage.ensure_bucket_exists()
        await storage.upload_bytes(storage_key, data, content_type=mime_type)

        clean_alt = (alt_text or os.path.splitext(filename or "")[0] or "Product Image")[:255]
        asset = ProductMediaAsset(
            id=asset_id,
            tenant_id=tenant_id,
            product_id=product_id,
            variant_id=variant_id,
            sourcing_draft_id=sourcing_draft_id,
            storage_key=storage_key,
            source_url=source_url,
            sha256=digest,
            mime_type=mime_type,
            byte_size=len(data),
            sort_order=sort_order,
            alt_text=clean_alt,
        )
        session.add(asset)
        await session.flush()
        presigned = await storage.generate_presigned_get_url(storage_key)
        return self._to_read(asset, presigned_url=presigned)

    async def ingest_external_urls(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        urls: Sequence[str],
        product_id: UUID | None = None,
        variant_id: UUID | None = None,
        sourcing_draft_id: UUID | None = None,
        alt_text: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        dns_resolver: MediaDnsResolver | None = None,
    ) -> list[MediaAssetRead]:
        """Download public HTTPS image URLs cleanly with SSRF protection and persist to MinIO."""
        allow_http = transport is not None
        results: list[MediaAssetRead] = []
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False, transport=transport
        ) as client:
            for idx, raw_url in enumerate(urls):
                safe_url = await validate_public_https_image_url(
                    raw_url,
                    dns_resolver=dns_resolver,
                    allow_http=allow_http,
                )
                response = await client.get(safe_url)
                response.raise_for_status()
                raw_bytes = response.content
                filename = os.path.basename(urlparse(safe_url).path) or f"image-{idx + 1}.jpg"
                asset = await self.upload_image_bytes(
                    session,
                    tenant_id,
                    filename=filename,
                    data=raw_bytes,
                    product_id=product_id,
                    variant_id=variant_id,
                    sourcing_draft_id=sourcing_draft_id,
                    alt_text=alt_text,
                    sort_order=idx,
                    source_url=safe_url,
                )
                results.append(asset)
        return results

    async def list_assets(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        product_id: UUID | None = None,
        sourcing_draft_id: UUID | None = None,
    ) -> list[MediaAssetRead]:
        stmt = select(ProductMediaAsset).where(ProductMediaAsset.tenant_id == tenant_id)
        if product_id is not None:
            stmt = stmt.where(ProductMediaAsset.product_id == product_id)
        if sourcing_draft_id is not None:
            stmt = stmt.where(ProductMediaAsset.sourcing_draft_id == sourcing_draft_id)
        rows = (
            (
                await session.execute(
                    stmt.order_by(
                        ProductMediaAsset.sort_order.asc(),
                        ProductMediaAsset.created_at.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [self._to_read(asset) for asset in rows]

    async def attach_draft_assets_to_product(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        sourcing_draft_id: UUID,
        product_id: UUID,
        variant_id: UUID | None = None,
    ) -> int:
        res = await session.execute(
            text(
                """
                UPDATE product_media_assets
                SET product_id = COALESCE(product_id, :pid),
                    variant_id = COALESCE(variant_id, :vid)
                WHERE tenant_id = :tid AND sourcing_draft_id = :did
                RETURNING id
                """
            ),
            {
                "tid": tenant_id,
                "did": sourcing_draft_id,
                "pid": product_id,
                "vid": variant_id,
            },
        )
        return len(res.all())

    async def load_asset_blobs(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        product_id: UUID | None = None,
        sourcing_draft_id: UUID | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Read image payloads for live marketplace uploaders (Shopify stagedUploads, etc.)."""
        stmt = select(ProductMediaAsset).where(ProductMediaAsset.tenant_id == tenant_id)
        if product_id is not None and sourcing_draft_id is not None:
            stmt = stmt.where(
                (ProductMediaAsset.product_id == product_id)
                | (ProductMediaAsset.sourcing_draft_id == sourcing_draft_id)
            )
        elif product_id is not None:
            stmt = stmt.where(ProductMediaAsset.product_id == product_id)
        elif sourcing_draft_id is not None:
            stmt = stmt.where(ProductMediaAsset.sourcing_draft_id == sourcing_draft_id)
        else:
            return []
        rows = (
            await session.execute(
                stmt.order_by(
                    ProductMediaAsset.sort_order.asc(),
                    ProductMediaAsset.created_at.asc(),
                ).limit(limit)
            )
        ).scalars().all()
        storage = self._get_storage()
        blobs: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        for asset in rows:
            if asset.sha256 in seen_sha:
                continue
            seen_sha.add(asset.sha256)
            ext = _IMAGE_EXT_BY_MIME.get(asset.mime_type, "jpg")
            data = await storage.download_bytes(asset.storage_key)
            blobs.append(
                {
                    "asset_id": str(asset.id),
                    "filename": f"{asset.sha256[:12]}.{ext}",
                    "mime_type": asset.mime_type,
                    "data": data,
                    "alt_text": asset.alt_text,
                }
            )
        return blobs

    async def read_asset_content(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        asset_id: UUID,
    ) -> tuple[bytes, str, str]:
        asset = (
            await session.execute(
                select(ProductMediaAsset).where(
                    ProductMediaAsset.tenant_id == tenant_id,
                    ProductMediaAsset.id == asset_id,
                )
            )
        ).scalar_one_or_none()
        if asset is None:
            raise AppError(
                slug="media-asset-not-found",
                status_code=404,
                code="ERR_MEDIA_ASSET_NOT_FOUND",
                title="Media Asset Not Found",
                detail=f"Media asset '{asset_id}' not found.",
            )
        storage = self._get_storage()
        data = await storage.download_bytes(asset.storage_key)
        ext = _IMAGE_EXT_BY_MIME.get(asset.mime_type, "jpg")
        return data, asset.mime_type, f"{asset.sha256[:12]}.{ext}"
