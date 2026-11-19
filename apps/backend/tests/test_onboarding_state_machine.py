from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient


async def _register_factory_owner(async_client: AsyncClient) -> str:
    suffix = uuid4().hex[:8]
    resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"onboard-{suffix}@factory.example.com",
            "password": "StrongPassword2026!",
            "full_name": "Onboard Owner",
            "tenant_name": f"LED Factory {suffix}",
            "tenant_slug": f"led-{suffix}",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["access_token"])


async def test_onboarding_linear_transitions_and_invalid_skip_422(
    async_client: AsyncClient,
) -> None:
    token = await _register_factory_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}

    initial = await async_client.get("/api/v1/onboarding/state", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["current_stage"] == "WorkspaceCreated"
    assert initial.json()["preset_type"] == "hybrid_factory_b2b_b2c"

    bad_skip = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "LiveProduction"},
    )
    assert bad_skip.status_code == 422
    assert bad_skip.json()["code"] == "ERR_INVALID_ONBOARDING_TRANSITION"

    connecting = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={
            "target_stage": "ChannelConnecting",
            "step_metadata": {"channel": "whatsapp_coexistence"},
        },
    )
    assert connecting.status_code == 200
    assert connecting.json()["current_stage"] == "ChannelConnecting"

    verified = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelVerified", "step_metadata": {"webhook_verified": True}},
    )
    assert verified.status_code == 200
    assert verified.json()["current_stage"] == "ChannelVerified"

    denied_shortcut = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "AISandboxReady", "skip_catalog_for_b2b": False},
    )
    assert denied_shortcut.status_code == 422

    for next_stage in ("CatalogSyncing", "AISandboxReady", "LiveProduction"):
        res = await async_client.post(
            "/api/v1/onboarding/advance",
            headers=headers,
            json={"target_stage": next_stage},
        )
        assert res.status_code == 200, res.text
        assert res.json()["current_stage"] == next_stage

    after_live = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "CatalogSyncing"},
    )
    assert after_live.status_code == 422


async def test_onboarding_pure_b2b_shortcut_channel_verified_to_ai_sandbox(
    async_client: AsyncClient,
) -> None:
    token = await _register_factory_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}

    preset_resp = await async_client.patch(
        "/api/v1/onboarding/preset",
        headers=headers,
        json={"preset_type": "pure_b2b_inquiry"},
    )
    assert preset_resp.status_code == 200
    assert preset_resp.json()["preset_type"] == "pure_b2b_inquiry"

    await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelConnecting"},
    )
    await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelVerified"},
    )

    shortcut_resp = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "AISandboxReady"},
    )
    assert shortcut_resp.status_code == 200
    assert shortcut_resp.json()["current_stage"] == "AISandboxReady"
    assert "AISandboxReady" in shortcut_resp.json()["completed_steps"]

    live_resp = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "LiveProduction"},
    )
    assert live_resp.status_code == 200
    assert live_resp.json()["current_stage"] == "LiveProduction"
