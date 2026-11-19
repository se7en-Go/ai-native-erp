from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND / "tests" / "fixtures" / "hybrid_factory_eval_100.json"


def test_hybrid_factory_evaluation_uses_all_locked_gates() -> None:
    # Start a fresh process; do not depend on app-wide ORM imports from another test.
    cli = subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "run_ai_evals.py"), "--fixture", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    thresholds = json.loads(FIXTURE.read_text())["gating_thresholds"]
    report, _ = json.JSONDecoder().raw_decode(cli.stdout)
    assert report["total_cases"] == 100
    assert report["category_counts"] == {
        "b2c_wismo": 20,
        "strict_led_rag": 20,
        "b2b_oem_rfq": 20,
        "compliance_trap": 20,
        "abstention_and_l0": 20,
    }
    assert report["postgres_major"] == 17 and report["pgvector_version"]
    assert report["redis_cache_verified"]
    assert report["numeric_hallucination_rate"] == thresholds["numeric_hallucination_rate_max"]
    for score in (
        "faq_grounded_accuracy",
        "abstention_recall",
        "compliance_block_rate",
        "l0_lock_accuracy",
    ):
        assert report[score] >= thresholds[f"{score}_min"], report["failures"]
    assert report["passed_all_gates"], report["failures"]


async def test_eval_refuses_missing_cases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(BACKEND / "scripts"))
    harness = importlib.import_module("run_ai_evals")
    fixture = json.loads(FIXTURE.read_bytes())
    fixture["cases"].pop()
    mutated = tmp_path / "incomplete.json"
    mutated.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="100"):
        await harness.run_evaluation(fixture_path=mutated)
