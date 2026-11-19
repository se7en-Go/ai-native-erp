from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from typing import Protocol
from uuid import UUID

import boto3  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.config import Settings
from erp_core.shared.errors import AppError


class KekProvider(Protocol):
    async def wrap_dek(self, tenant_id: UUID, dek: bytes) -> bytes: ...
    async def unwrap_dek(self, tenant_id: UUID, wrapped_dek: bytes) -> bytes: ...


class LocalAesKekProvider:
    """Development/self-hosted Master KEK wrapping per-tenant 256-bit DEKs via AES-256-GCM."""

    def __init__(self, master_kek_hex: str) -> None:
        kek_bytes = bytes.fromhex(master_kek_hex)
        if len(kek_bytes) != 32:
            raise ValueError("MASTER_KEK_HEX must decode to exactly 32 bytes (64 hex chars)")
        self._kek_aes = AESGCM(kek_bytes)

    async def wrap_dek(self, tenant_id: UUID, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        aad = f"kek-wrap:{tenant_id}".encode()
        wrapped = self._kek_aes.encrypt(nonce, dek, aad)
        return nonce + wrapped

    async def unwrap_dek(self, tenant_id: UUID, wrapped_dek: bytes) -> bytes:
        if len(wrapped_dek) < 28:
            raise AppError(
                slug="corrupt-wrapped-dek",
                title="Corrupt Wrapped DEK",
                status_code=500,
                detail="Stored tenant encrypted DEK payload is malformed",
                code="ERR_CORRUPT_DEK",
            )
        nonce = wrapped_dek[:12]
        ciphertext_with_tag = wrapped_dek[12:]
        aad = f"kek-wrap:{tenant_id}".encode()
        try:
            return self._kek_aes.decrypt(nonce, ciphertext_with_tag, aad)
        except InvalidTag as exc:
            raise AppError(
                slug="kek-unwrap-failed",
                title="Master KEK Unwrap Failed",
                status_code=403,
                detail="Failed to unwrap tenant DEK under Master KEK",
                code="ERR_KEK_UNWRAP_FAILED",
            ) from exc


class AwsKmsKekProvider:
    """Production AWS KMS KEK wrapper using EncryptionContext bound to tenant_id."""

    def __init__(self, key_id: str, region_name: str) -> None:
        self._key_id = key_id
        self._kms = boto3.client("kms", region_name=region_name)

    async def wrap_dek(self, tenant_id: UUID, dek: bytes) -> bytes:
        def _encrypt() -> bytes:
            resp = self._kms.encrypt(
                KeyId=self._key_id,
                Plaintext=dek,
                EncryptionContext={"tenant_id": str(tenant_id)},
            )
            return bytes(resp["CiphertextBlob"])

        return await asyncio.to_thread(_encrypt)

    async def unwrap_dek(self, tenant_id: UUID, wrapped_dek: bytes) -> bytes:
        def _decrypt() -> bytes:
            resp = self._kms.decrypt(
                CiphertextBlob=wrapped_dek,
                EncryptionContext={"tenant_id": str(tenant_id)},
            )
            return bytes(resp["Plaintext"])

        return await asyncio.to_thread(_decrypt)


class _DekLruCache:
    """Thread/async-safe bounded LRU cache with strict 5-minute maximum TTL for plaintext DEKs."""

    def __init__(self, maxsize: int = 1024, ttl_seconds: int = 300) -> None:
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._store: OrderedDict[tuple[UUID, int], tuple[bytes, bytes, float]] = OrderedDict()

    def get(self, tenant_id: UUID, key_version: int, wrapped_dek: bytes) -> bytes | None:
        key = (tenant_id, key_version)
        entry = self._store.get(key)
        if entry is None:
            return None
        dek, stored_wrapped, expires_at = entry
        if stored_wrapped != wrapped_dek or time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return dek

    def put(
        self,
        tenant_id: UUID,
        key_version: int,
        dek: bytes,
        wrapped_dek: bytes,
    ) -> None:
        key = (tenant_id, key_version)
        expires_at = time.monotonic() + self._ttl_seconds
        self._store[key] = (dek, wrapped_dek, expires_at)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def evict_tenant(self, tenant_id: UUID) -> None:
        matching = [k for k in self._store if k[0] == tenant_id]
        for k in matching:
            self._store.pop(k, None)


class EnvelopeCryptoService:
    """Two-tier KMS Envelope Encryption (Master KEK -> Per-Tenant 256-bit DEK -> AES-256-GCM)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache = _DekLruCache(maxsize=1024, ttl_seconds=settings.DEK_CACHE_TTL_SECONDS)
        if settings.KMS_PROVIDER == "aws_kms":
            self._kek_provider: KekProvider = AwsKmsKekProvider(
                key_id=settings.KMS_KEY_ID,
                region_name=settings.S3_REGION,
            )
        elif settings.KMS_PROVIDER == "local_aes":
            self._kek_provider = LocalAesKekProvider(settings.MASTER_KEK_HEX)
        else:
            raise AppError(
                slug="unsupported-kms-provider",
                title="Unsupported KMS Provider",
                status_code=500,
                detail=f"Unsupported KMS_PROVIDER: {settings.KMS_PROVIDER}",
                code="ERR_UNSUPPORTED_KMS_PROVIDER",
            )

    @staticmethod
    def _build_aad(tenant_id: UUID, account_id: UUID, cred_type: str) -> bytes:
        return f"{tenant_id}:{account_id}:{cred_type}".encode()

    async def provision_tenant_dek(self, session: AsyncSession, tenant_id: UUID) -> int:
        existing_ver = await session.scalar(
            text("SELECT key_version FROM tenant_keys WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        if existing_ver is not None:
            return int(existing_ver)

        plaintext_dek = os.urandom(32)
        wrapped_dek = await self._kek_provider.wrap_dek(tenant_id, plaintext_dek)
        await session.execute(
            text(
                "INSERT INTO tenant_keys (tenant_id, kek_key_arn, encrypted_dek, key_version) "
                "VALUES (:tid, :kek, :edek, 1) "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {
                "tid": str(tenant_id),
                "kek": self._settings.KMS_KEY_ID,
                "edek": wrapped_dek,
            },
        )
        saved_ver = await session.scalar(
            text("SELECT key_version FROM tenant_keys WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        ver = int(saved_ver or 1)
        if ver == 1:
            self._cache.put(tenant_id, ver, plaintext_dek, wrapped_dek)
        return ver

    async def _resolve_tenant_dek(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        key_version: int | None = None,
    ) -> tuple[bytes, int]:
        """Resolve a DEK only when tenant_keys still holds that wrapped key.

        The in-process cache skips KEK unwrap. It is not an authorization cache:
        every caller, including another process object, re-reads the committed
        key row before a cached DEK may be used. There is no separate Redis
        invalidation key in the crypto contract.
        """
        if key_version is None:
            row = (
                await session.execute(
                    text(
                        "SELECT encrypted_dek, key_version FROM tenant_keys WHERE tenant_id = :tid"
                    ),
                    {"tid": str(tenant_id)},
                )
            ).first()
        else:
            row = (
                await session.execute(
                    text(
                        "SELECT encrypted_dek, key_version FROM tenant_keys "
                        "WHERE tenant_id = :tid AND key_version = :kv"
                    ),
                    {"tid": str(tenant_id), "kv": key_version},
                )
            ).first()

        if row is None:
            self._cache.evict_tenant(tenant_id)
            raise AppError(
                slug="tenant-key-revoked",
                title="Tenant Encryption Key Missing or Revoked",
                status_code=403,
                detail="No active encryption DEK exists for this tenant",
                code="ERR_TENANT_KEY_REVOKED",
            )

        wrapped = bytes(row.encrypted_dek)
        resolved_ver = int(row.key_version)
        cached_hit = self._cache.get(tenant_id, resolved_ver, wrapped)
        if cached_hit is not None:
            return cached_hit, resolved_ver

        dek = await self._kek_provider.unwrap_dek(tenant_id, wrapped)
        self._cache.put(tenant_id, resolved_ver, dek, wrapped)
        return dek, resolved_ver

    async def encrypt_secret(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        account_id: UUID,
        cred_type: str,
        plaintext: str,
    ) -> tuple[bytes, bytes, bytes, int]:
        await self.provision_tenant_dek(session, tenant_id)
        dek, key_version = await self._resolve_tenant_dek(session, tenant_id)
        nonce = os.urandom(12)
        aad = self._build_aad(tenant_id, account_id, cred_type)
        encrypted = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)
        ciphertext = encrypted[:-16]
        auth_tag = encrypted[-16:]
        return ciphertext, nonce, auth_tag, key_version

    async def decrypt_secret(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        account_id: UUID,
        cred_type: str,
        ciphertext: bytes,
        nonce: bytes,
        auth_tag: bytes,
        key_version: int,
    ) -> str:
        dek, _ = await self._resolve_tenant_dek(session, tenant_id, key_version=key_version)
        aad = self._build_aad(tenant_id, account_id, cred_type)
        try:
            plaintext_bytes = AESGCM(dek).decrypt(nonce, ciphertext + auth_tag, aad)
        except InvalidTag as exc:
            raise AppError(
                slug="credential-decryption-failed",
                title="Credential Decryption Failed",
                status_code=403,
                detail="GCM authentication tag or AAD verification failed",
                code="ERR_INVALID_CREDENTIAL_TAG",
            ) from exc
        return plaintext_bytes.decode("utf-8")

    async def crypto_shred_tenant(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._cache.evict_tenant(tenant_id)
        await session.execute(
            text("DELETE FROM channel_credentials WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        await session.execute(
            text("DELETE FROM tenant_keys WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        await session.execute(
            text(
                "UPDATE tenants SET is_active = false, shredded_at = now(), updated_at = now() "
                "WHERE id = :tid"
            ),
            {"tid": str(tenant_id)},
        )
