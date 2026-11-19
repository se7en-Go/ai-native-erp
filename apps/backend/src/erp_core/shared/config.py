from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    DATABASE_MIGRATOR_URL: str = (
        "postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas"
    )
    DATABASE_RUNTIME_URL: str = (
        "postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:6432/erp_saas"
    )

    REDIS_URL: str = "redis://localhost:6379/0"
    OPENAI_API_KEY: str = Field(default="", repr=False)
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    ONBOARDING_DNS_NAMESERVER: str = ""
    ONBOARDING_DNS_PORT: int = Field(default=53, ge=1, le=65535)
    ANTHROPIC_API_KEY: str = Field(default="", repr=False)
    GEMINI_API_KEY: str = Field(default="", repr=False)
    DEEPSEEK_API_KEY: str = Field(default="", repr=False)
    STRIPE_SECRET_KEY: str = Field(default="", repr=False)
    STRIPE_WEBHOOK_SECRET: str = Field(default="", repr=False)
    # Phase 3C compliant lead collection (BYOK; empty means the source is disabled).
    GOOGLE_PLACES_API_KEY: str = Field(default="", repr=False)

    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_BUCKET: str = "erp-assets"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_REGION: str = "us-east-1"

    KMS_PROVIDER: str = "local_aes"
    MASTER_KEK_HEX: str = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    KMS_KEY_ID: str = "local-dev-kek-v1"

    JWT_SECRET: str = "dev-only-jwt-secret-change-me-minimum-32-bytes"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # NoDecode keeps the raw env string so the validator below can accept both
    # `a,b` (compose/.env style) and `["a","b"]`. Without it pydantic-settings
    # JSON-decodes complex fields before validation and a comma list aborts
    # startup with SettingsError.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:3001"]
    )
    DEK_CACHE_TTL_SECONDS: int = 300

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                return [str(item) for item in parsed]
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return ["http://localhost:3000", "http://localhost:3001"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
