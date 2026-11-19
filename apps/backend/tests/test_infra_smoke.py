from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from httpx import AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "apps" / "backend" / "src" / "erp_core"


async def test_healthz_and_readyz_endpoints(async_client: AsyncClient) -> None:
    health_resp = await async_client.get("/healthz")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    ready_resp = await async_client.get("/readyz")
    assert ready_resp.status_code == 200
    body = ready_resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["redis"] == "ok"


def test_import_linter_contracts_pass() -> None:
    lint_imports_bin = Path(sys.executable).parent / "lint-imports"
    result = subprocess.run(
        [str(lint_imports_bin), "--config", str(REPO_ROOT / "infra" / "importlinter.ini")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import-linter failed:\n{result.stdout}\n{result.stderr}"


def test_no_cross_module_orm_model_imports() -> None:
    domain_dirs = {
        "tenancy",
        "channels",
        "identity",
        "catalog",
        "crm",
        "orders",
        "finance",
        "ai_engine",
        "billing",
    }
    violations: list[str] = []
    for py_file in SRC_ROOT.rglob("*.py"):
        rel_parts = py_file.relative_to(SRC_ROOT).parts
        if not rel_parts or rel_parts[0] not in domain_dirs:
            continue
        owner_module = rel_parts[0]
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for target in domain_dirs - {owner_module}:
                    if node.module == f"erp_core.{target}.models" or node.module.startswith(
                        f"erp_core.{target}.models."
                    ):
                        violations.append(f"{py_file}: imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for target in domain_dirs - {owner_module}:
                        if alias.name == f"erp_core.{target}.models" or alias.name.startswith(
                            f"erp_core.{target}.models."
                        ):
                            violations.append(f"{py_file}: imports {alias.name}")
    assert not violations, f"Cross-module ORM model imports forbidden: {violations}"


def test_canonical_enums_registered() -> None:
    from erp_core.shared import enums

    assert enums.UserRole.OWNER == "owner"
    assert enums.UserRole.FINANCE == "finance"
    assert enums.OnboardingStage.WORKSPACE_CREATED == "WorkspaceCreated"
    assert enums.OnboardingStage.LIVE_PRODUCTION == "LiveProduction"
    assert enums.TrackingProvider.TRACK17 == "17track"
    assert enums.AutonomyLevel.L1_PROPOSAL == "L1_proposal"
    assert enums.UsageMetricType.TRACKING_QUOTA == "tracking_quota"
