from __future__ import annotations

import pytest

from erp_core.shared.config import Settings


def test_cors_origins_accepts_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose/.env style lists must not require JSON encoding.

    Regression: pydantic-settings JSON-decodes complex fields before validators
    run, so `CORS_ORIGINS=a,b` used to abort startup with SettingsError.
    """
    monkeypatch.setenv("CORS_ORIGINS", "https://erp.example.com,https://api.erp.example.com")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.CORS_ORIGINS == [
        "https://erp.example.com",
        "https://api.erp.example.com",
    ]


def test_cors_origins_accepts_json_array_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000"]')

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.CORS_ORIGINS == ["http://localhost:3000"]


def test_cors_origins_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.CORS_ORIGINS == ["http://localhost:3000", "http://localhost:3001"]
