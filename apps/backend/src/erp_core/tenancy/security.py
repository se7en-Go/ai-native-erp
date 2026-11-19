from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel

from erp_core.shared.config import Settings
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError

_password_hasher = PasswordHasher()


class TokenPayload(BaseModel):
    sub: UUID
    tid: UUID
    role: UserRole
    email: str
    type: Literal["access", "refresh"]
    jti: str


def hash_password(plain_password: str) -> str:
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bool(_password_hasher.verify(password_hash, plain_password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: UserRole,
    email: str,
    settings: Settings,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role.value,
        "email": email,
        "type": "access",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return str(jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM))


def create_refresh_token(
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: UserRole,
    email: str,
    settings: Settings,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role.value,
        "email": email,
        "type": "refresh",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    }
    return str(jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM))


def decode_token(
    token: str,
    settings: Settings,
    *,
    expected_type: Literal["access", "refresh"] = "access",
) -> TokenPayload:
    try:
        raw = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "tid", "role", "email", "type", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise AppError(
            slug="invalid-token",
            title="Invalid or Expired Token",
            status_code=401,
            detail="Authentication token is invalid, malformed, or expired",
            code="ERR_INVALID_TOKEN",
        ) from exc

    if raw.get("type") != expected_type:
        raise AppError(
            slug="invalid-token-type",
            title="Invalid Token Type",
            status_code=401,
            detail=f"Expected {expected_type} token",
            code="ERR_INVALID_TOKEN",
        )

    try:
        return TokenPayload(
            sub=UUID(str(raw["sub"])),
            tid=UUID(str(raw["tid"])),
            role=UserRole(str(raw["role"])),
            email=str(raw["email"]),
            type=expected_type,
            jti=str(raw.get("jti", "")),
        )
    except (ValueError, KeyError) as exc:
        raise AppError(
            slug="invalid-token-claims",
            title="Invalid Token Claims",
            status_code=401,
            detail="Token claims failed validation",
            code="ERR_INVALID_TOKEN",
        ) from exc
