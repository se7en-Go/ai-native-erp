from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PRODUCTION_ENVS = frozenset({"production", "prod"})
_DEV_JWT_SECRET_PREFIX = "dev-only-jwt-secret"
_DEV_MASTER_KEK_PREFIX = "0001020304050607"
_DEV_RUNTIME_DB_MARKER = "runtime_dev_pass"
_DEV_MIGRATOR_DB_MARKER = "migrator_dev_pass"
_DEV_S3_SECRET = "minioadmin"
_MIN_JWT_SECRET_LENGTH = 32


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

    # Optional production telemetry (Track 8). Empty means "strictly disabled":
    # `dispatch_error_event` / `dispatch_trace_span` then never touch the network.
    SENTRY_DSN: str = Field(default="", repr=False)
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # Admin ops health thresholds (`GET /api/v1/ops/health`).
    OPS_QUEUE_WARN_DEPTH: int = Field(default=100, ge=1)
    OPS_QUEUE_CRIT_DEPTH: int = Field(default=1000, ge=1)

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

    @model_validator(mode="after")
    def validate_ops_thresholds(self) -> Settings:
        """The critical backlog ceiling must never sit below the warn ceiling."""
        if self.OPS_QUEUE_CRIT_DEPTH < self.OPS_QUEUE_WARN_DEPTH:
            raise ValueError(
                "OPS_QUEUE_CRIT_DEPTH must be greater than or equal to OPS_QUEUE_WARN_DEPTH"
            )
        return self

    @model_validator(mode="after")
    def reject_development_secrets_in_production(self) -> Settings:
        """Fail closed: a production boot must never inherit a development secret.

        ``APP_ENV=development``/``test`` keeps every shipped default so local runs and
        pytest stay seamless; ``production``/``prod`` refuses to start instead of
        silently running with a publicly known KEK, JWT key or DB password.
        """
        if self.APP_ENV.strip().lower() not in _PRODUCTION_ENVS:
            return self
        problems: list[str] = []
        jwt_secret = self.JWT_SECRET.strip()
        if not jwt_secret or jwt_secret.startswith(_DEV_JWT_SECRET_PREFIX):
            problems.append("JWT_SECRET still uses the development default")
        if len(jwt_secret) < _MIN_JWT_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET must be at least {_MIN_JWT_SECRET_LENGTH} characters long"
            )
        if self.MASTER_KEK_HEX.strip().lower().startswith(_DEV_MASTER_KEK_PREFIX):
            problems.append("MASTER_KEK_HEX still uses the development default")
        if _DEV_RUNTIME_DB_MARKER in self.DATABASE_RUNTIME_URL:
            problems.append("DATABASE_RUNTIME_URL still uses the development password")
        if _DEV_MIGRATOR_DB_MARKER in self.DATABASE_MIGRATOR_URL:
            problems.append("DATABASE_MIGRATOR_URL still uses the development password")
        if self.S3_SECRET_KEY.strip() == _DEV_S3_SECRET:
            problems.append("S3_SECRET_KEY still uses the development default")
        if problems:
            raise ValueError(
                "Refusing to start with development defaults while APP_ENV="
                f"'{self.APP_ENV}': " + "; ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
