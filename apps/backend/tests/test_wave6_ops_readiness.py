"""Wave 6 ops readiness: offline migration SQL, `/readyz` probes, non-root widget image.

These tests are hermetic: the Alembic check only *renders* SQL (`--sql` offline mode),
so no PostgreSQL/Redis server is contacted, and `/readyz` is exercised against a
`create_app()` instance whose `redis` / `session_factory` state is replaced with
fakes so both the ready and degraded branches are covered deterministically.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
WIDGET_DOCKERFILE = REPO_ROOT / "infra" / "docker" / "widget.Dockerfile"

#: 0003 (dual-mode ERP) tables whose fail-closed RLS must survive offline rendering.
WAVE3_0003_RLS_TABLES = ("products", "orders", "payment_records")
#: 0013 (Wave 2 finance hardening) table created with append-only RLS.
WAVE6_0013_RLS_TABLES = ("payable_payments",)

OFFLINE_SQL_TIMEOUT_SECONDS = 600


def _alembic_env() -> dict[str, str]:
    """Environment for the Alembic CLI: settings-free placeholders, never dialed."""
    env = os.environ.copy()
    env.setdefault("APP_ENV", "test")
    env.setdefault(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas",
    )
    return env


def _run_offline_sql() -> subprocess.CompletedProcess[str]:
    """Render the full 0001 -> head offline migration script (no DB connection)."""
    return subprocess.run(
        # `script_location = alembic` is resolved relative to the working directory,
        # so the CLI must run from apps/backend where alembic.ini lives.
        [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI.name, "upgrade", "head", "--sql"],
        cwd=str(BACKEND_ROOT),
        env=_alembic_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=OFFLINE_SQL_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope="module")
def offline_sql() -> str:
    """Offline SQL for the whole migration chain, rendered exactly once per module."""
    result = _run_offline_sql()
    assert result.returncode == 0, (
        "alembic offline SQL rendering failed:"
        f"\nstdout:\n{result.stdout[-4000:]}"
        f"\nstderr:\n{result.stderr[-8000:]}"
    )
    return result.stdout


def test_alembic_offline_sql_reaches_head_without_error(offline_sql: str) -> None:
    """`upgrade head --sql` must exit 0 and render every revision up to head."""
    rendered = re.findall(r"^-- Running upgrade \S* -> (\S+)$", offline_sql, re.MULTILINE)
    version_files = sorted((BACKEND_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(rendered) == len(version_files), (
        f"expected one rendered step per migration file ({len(version_files)}), "
        f"got {len(rendered)}: {rendered}"
    )
    assert rendered[0] == "0001_tenancy_rls_and_audit"
    assert "0003" in rendered
    assert rendered[-1] == "0019_analytics_im_sourcing"
    # Head is stamped only after the last migration body rendered successfully.
    assert "UPDATE alembic_version SET version_num='0019_analytics_im_sourcing'" in offline_sql
    assert offline_sql.rstrip().endswith("COMMIT;")


@pytest.mark.parametrize("table", WAVE3_0003_RLS_TABLES)
def test_offline_sql_enables_fail_closed_rls_for_0003_tables(offline_sql: str, table: str) -> None:
    """0003 emits ENABLE + FORCE RLS even without a live `pg_roles` catalog lookup."""
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in offline_sql
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in offline_sql


@pytest.mark.parametrize("table", WAVE6_0013_RLS_TABLES)
def test_offline_sql_enables_fail_closed_rls_for_0013_tables(offline_sql: str, table: str) -> None:
    """0013's append-only finance table keeps its RLS in the offline script too."""
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in offline_sql
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in offline_sql
    assert f"REVOKE ALL ON {table} FROM app_runtime;" in offline_sql
    assert f"GRANT SELECT, INSERT ON {table} TO app_runtime;" in offline_sql


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in for `AsyncSession` used by `system_session`."""

    def __init__(self, *, scalar_value: int | None, error: Exception | None) -> None:
        self._scalar_value = scalar_value
        self._error = error

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, statement: Any) -> None:
        if self._error is not None:
            raise self._error

    async def scalar(self, statement: Any) -> int | None:
        if self._error is not None:
            raise self._error
        return self._scalar_value


class _FakeRedis:
    def __init__(self, *, pong: bool = True, error: Exception | None = None) -> None:
        self._pong = pong
        self._error = error

    async def ping(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._pong


def _fake_session_factory(*, scalar_value: int | None = 1, error: Exception | None = None) -> Any:
    def _factory() -> _FakeSession:
        return _FakeSession(scalar_value=scalar_value, error=error)

    return _factory


def _readyz_app(
    *,
    redis: _FakeRedis,
    session_factory: Any,
) -> Any:
    """`create_app()` without its lifespan: only the two probed handles are injected."""
    from erp_core.api_main import create_app

    app = create_app()
    app.state.redis = redis
    app.state.session_factory = session_factory
    return app


async def _get_readyz(app: Any) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/readyz")
    return response.status_code, response.json()


async def test_readyz_returns_200_when_redis_and_postgres_are_healthy() -> None:
    app = _readyz_app(redis=_FakeRedis(pong=True), session_factory=_fake_session_factory())

    status_code, body = await _get_readyz(app)

    assert status_code == 200
    assert body == {"status": "ready", "checks": {"redis": "ok", "postgres": "ok"}}


async def test_readyz_returns_503_when_redis_connection_fails() -> None:
    app = _readyz_app(
        redis=_FakeRedis(error=RedisConnectionError("redis://localhost:6379 down")),
        session_factory=_fake_session_factory(),
    )

    status_code, body = await _get_readyz(app)

    assert status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "error: ConnectionError"
    assert body["checks"]["postgres"] == "ok"


async def test_readyz_returns_503_when_postgres_select_fails() -> None:
    app = _readyz_app(
        redis=_FakeRedis(pong=True),
        session_factory=_fake_session_factory(error=RuntimeError("pool exhausted")),
    )

    status_code, body = await _get_readyz(app)

    assert status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["postgres"] == "error: RuntimeError"


@pytest.fixture(scope="module")
def widget_dockerfile() -> str:
    return Path(WIDGET_DOCKERFILE).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runtime_stage(widget_dockerfile: str) -> str:
    """Only the final (runtime) stage — earlier stages legitimately run as root Node builds."""
    match = re.search(r"^FROM\s+\S+\s+AS\s+runtime\s*$", widget_dockerfile, re.MULTILINE)
    assert match is not None, "widget.Dockerfile must declare a `runtime` stage"
    return widget_dockerfile[match.start() :]


def test_widget_runtime_uses_unprivileged_nginx(runtime_stage: str) -> None:
    assert runtime_stage.startswith("FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime")
    assert "FROM nginx:1.27-alpine" not in runtime_stage


def test_widget_runtime_declares_non_root_user(runtime_stage: str) -> None:
    user_lines = [
        line.strip() for line in runtime_stage.splitlines() if line.strip().startswith("USER ")
    ]
    assert user_lines, "runtime stage must declare an explicit USER"
    for line in user_lines:
        user = line.split(maxsplit=1)[1].split(":", maxsplit=1)[0]
        assert user != "root"
        assert user.isdigit(), f"expected a numeric unprivileged uid, got USER {user}"
        assert int(user) != 0, "runtime must never run as uid 0"


def test_widget_runtime_copies_assets_owned_by_uid_101(runtime_stage: str) -> None:
    assert (
        "COPY --chown=101:101 infra/docker/widget-nginx.conf "
        "/etc/nginx/conf.d/default.conf" in runtime_stage
    )
    assert (
        "COPY --from=builder --chown=101:101 "
        "/repo/apps/widget/dist/livechat-widget.iife.js "
        "/usr/share/nginx/html/livechat-widget.iife.js" in runtime_stage
    )


def test_widget_runtime_keeps_port_and_healthcheck(runtime_stage: str) -> None:
    assert "EXPOSE 3001" in runtime_stage
    assert (
        re.search(r"HEALTHCHECK[^\n]*\n[^\n]*http://127\.0\.0\.1:3001/healthz", runtime_stage)
        is not None
    )


def test_widget_keeps_gzip_budget_guard(widget_dockerfile: str) -> None:
    assert "ARG WIDGET_GZIP_LIMIT_BYTES=35840" in widget_dockerfile
    assert "gzip -c apps/widget/dist/livechat-widget.iife.js" in widget_dockerfile
    assert 'if [ "$bytes" -ge "$WIDGET_GZIP_LIMIT_BYTES" ]' in widget_dockerfile
