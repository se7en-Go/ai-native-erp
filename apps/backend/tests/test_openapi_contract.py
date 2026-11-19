from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_FILE = REPO_ROOT / "contracts" / "openapi.json"


def test_export_openapi_contract_paths_without_frontend_codegen() -> None:
    """Backend contract export only.

    The plan's Task 1.7 also generates apps/web via pnpm. That tree is owned by
    the concurrent frontend agent and is outside this batch's allowlist.
    """
    export_script = REPO_ROOT / "apps" / "backend" / "scripts" / "export_openapi.py"
    assert export_script.exists(), "export_openapi.py script is missing"

    export_proc = subprocess.run(
        [sys.executable, str(export_script)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert export_proc.returncode == 0, f"export_openapi failed:\n{export_proc.stderr}"
    assert CONTRACTS_FILE.exists()

    spec = json.loads(CONTRACTS_FILE.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.1")
    for expected_path in (
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/tenants/memberships",
        "/api/v1/tenancy/audit-events",
        "/api/v1/tenancy/gdpr/customers/{customer_id}/export",
        "/api/v1/onboarding/state",
        "/api/v1/onboarding/advance",
        "/api/v1/ai/proposals/{proposal_id}/edit-approve",
        "/api/v1/ai/assistant/chat",
        "/api/v1/billing/wallet",
        "/api/v1/orders/warehouses",
    ):
        assert expected_path in spec["paths"], f"Missing OpenAPI path: {expected_path}"

    second = subprocess.run(
        [sys.executable, str(export_script)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    again = json.loads(CONTRACTS_FILE.read_text(encoding="utf-8"))
    assert again == spec
