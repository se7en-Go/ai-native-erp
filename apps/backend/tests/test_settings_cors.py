from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from erp_core.api_main import create_app
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


async def test_scoped_cors_allows_widget_preflight_without_loosening_auth_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000"]')
    app = create_app()
    external_origin = "https://external-merchant.example"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        widget_opt = await client.options(
            "/api/v1/widget/session",
            headers={
                "Origin": external_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization",
            },
        )
        assert widget_opt.status_code == 204
        assert widget_opt.headers.get("access-control-allow-origin") == external_origin
        assert "access-control-allow-credentials" not in {
            k.lower() for k in widget_opt.headers
        }

        auth_opt = await client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": external_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert auth_opt.status_code == 400
