"""Zero-dependency Prometheus metrics, request correlation and optional telemetry.

This module lives in `erp_core.shared`, which `infra/importlinter.ini`
(`shared_no_domain_imports`) forbids from importing any domain module. It
therefore depends only on the standard library plus Starlette/FastAPI ASGI
types, and it never opens a database connection or reads a tenant row.

Three responsibilities, deliberately small:

1. :class:`ObservabilityRegistry` — an in-process counter/histogram/gauge store
   that renders valid Prometheus text exposition (version `0.0.4`). It is
   lock-guarded so the async event loop and worker threads can observe
   concurrently, and its label set is bounded by route *templates*, never raw
   URLs.
2. :class:`ObservabilityMiddleware` — a **pure ASGI** middleware (never
   `BaseHTTPMiddleware`, which buffers response bodies) so SSE endpoints such as
   `/api/v1/inbox/stream` keep streaming chunk-by-chunk. It echoes or mints an
   `X-Request-ID` for every request and records latency/status.
3. :func:`dispatch_error_event` / :func:`dispatch_trace_span` — fire-and-forget
   Sentry/OTLP hooks that are a strict no-op unless `SENTRY_DSN` or
   `OTEL_EXPORTER_OTLP_ENDPOINT` is configured, and that can never raise into
   request handling even when the collector is unreachable.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import urlparse

from fastapi import APIRouter
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROMETHEUS_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"
DURATION_BUCKETS: Final[tuple[float, ...]] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
SERVICE_NAME: Final[str] = "erp-core"

_MAX_PATH_TEMPLATE_LENGTH: Final[int] = 200
_REQUEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,64}")
_UUID_SEGMENT: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_INTEGER_SEGMENT: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,20}")
# Opaque ids/tokens: long, and contain both letters and digits (e.g. `sk_live_ab12...`).
_TOKEN_SEGMENT: Final[re.Pattern[str]] = re.compile(r"(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9_-]{16,}")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_float(value: float) -> str:
    if value != value:  # NaN
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    return f"{value:.9g}"


def _labels(pairs: Iterable[tuple[str, str]]) -> str:
    rendered = ",".join(f'{name}="{_escape_label(value)}"' for name, value in pairs)
    return f"{{{rendered}}}" if rendered else ""


def collapse_path(path: str) -> str:
    """Collapse identifier-looking path segments so metric cardinality stays bounded."""
    collapsed = [
        "{id}" if _identifier_segment(segment) else segment for segment in path.split("/")
    ]
    rendered = "/".join(collapsed)
    return rendered[:_MAX_PATH_TEMPLATE_LENGTH]


def _identifier_segment(segment: str) -> bool:
    return bool(_UUID_SEGMENT.fullmatch(segment) or _integer_or_token_segment(segment))


def _integer_or_token_segment(segment: str) -> bool:
    return bool(_INTEGER_SEGMENT.fullmatch(segment) or _TOKEN_SEGMENT.fullmatch(segment))


def route_path_template(scope: Mapping[str, Any]) -> str:
    """Prefer the matched Starlette route template, else collapse the raw path."""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template[:_MAX_PATH_TEMPLATE_LENGTH]
    path = scope.get("path")
    if not isinstance(path, str) or not path:
        return "/"
    return collapse_path(path)


def extract_request_id(scope: Mapping[str, Any]) -> str:
    """Reuse a well-formed inbound `X-Request-ID`, otherwise mint a UUID4."""
    headers = scope.get("headers") or []
    for raw_name, raw_value in headers:
        name = str(raw_name.decode("latin-1")).lower()
        if name != REQUEST_ID_HEADER.lower():
            continue
        candidate = str(raw_value.decode("latin-1")).strip()
        if _REQUEST_ID_PATTERN.fullmatch(candidate):
            return candidate
        break
    return str(uuid.uuid4())


@dataclass
class _HistogramSeries:
    bucket_counts: list[int] = field(default_factory=lambda: [0] * len(DURATION_BUCKETS))
    total: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        for index, bound in enumerate(DURATION_BUCKETS):
            if value <= bound:
                self.bucket_counts[index] += 1
                break
        self.total += value
        self.count += 1


class ObservabilityRegistry:
    """Thread-safe in-process Prometheus collector (counter + histogram + gauge)."""

    def __init__(self, buckets: tuple[float, ...] = DURATION_BUCKETS) -> None:
        self._buckets = buckets
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str, str], int] = {}
        self._histograms: dict[tuple[str, str], _HistogramSeries] = {}
        self._gauges: dict[str, int] = {}

    # -- recording ---------------------------------------------------------

    def observe_request(
        self,
        *,
        method: str,
        path_template: str,
        status: str | int,
        duration_seconds: float,
    ) -> None:
        key = (method.upper(), path_template, str(status))
        histogram_key = (method.upper(), path_template)
        duration = max(0.0, float(duration_seconds))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            series = self._histograms.get(histogram_key)
            if series is None:
                series = _HistogramSeries()
                self._histograms[histogram_key] = series
            series.observe(duration)

    def set_stream_backlog(self, stream: str, depth: int) -> None:
        with self._lock:
            self._gauges[stream] = int(depth)

    # -- inspection --------------------------------------------------------

    def request_count(self, *, method: str, path_template: str, status: str | int) -> int:
        with self._lock:
            return self._counters.get((method.upper(), path_template, str(status)), 0)

    def stream_backlog(self, stream: str) -> int | None:
        with self._lock:
            return self._gauges.get(stream)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()

    # -- rendering ---------------------------------------------------------

    def render(self) -> str:
        with self._lock:
            counters = dict(sorted(self._counters.items()))
            histograms = dict(sorted(self._histograms.items()))
            gauges = dict(sorted(self._gauges.items()))

        lines: list[str] = [
            "# HELP http_requests_total Total HTTP requests handled by the API.",
            "# TYPE http_requests_total counter",
        ]
        for (method, path_template, status), value in counters.items():
            labels = _labels(
                (
                    ("method", method),
                    ("path_template", path_template),
                    ("status", status),
                )
            )
            lines.append(f"http_requests_total{labels} {value}")

        lines.extend(
            (
                "# HELP http_request_duration_seconds HTTP request latency in seconds.",
                "# TYPE http_request_duration_seconds histogram",
            )
        )
        for (method, path_template), series in histograms.items():
            cumulative = 0
            for index, bound in enumerate(DURATION_BUCKETS):
                cumulative += series.bucket_counts[index]
                labels = _labels(
                    (
                        ("method", method),
                        ("path_template", path_template),
                        ("le", _format_float(bound)),
                    )
                )
                lines.append(f"http_request_duration_seconds_bucket{labels} {cumulative}")
            inf_labels = _labels(
                (
                    ("method", method),
                    ("path_template", path_template),
                    ("le", "+Inf"),
                )
            )
            lines.append(f"http_request_duration_seconds_bucket{inf_labels} {series.count}")
            base = _labels((("method", method), ("path_template", path_template)))
            lines.append(
                f"http_request_duration_seconds_sum{base} {_format_float(series.total)}"
            )
            lines.append(f"http_request_duration_seconds_count{base} {series.count}")

        lines.extend(
            (
                "# HELP erp_stream_backlog_messages Undelivered messages per Redis stream.",
                "# TYPE erp_stream_backlog_messages gauge",
            )
        )
        for stream, depth in gauges.items():
            lines.append(f"erp_stream_backlog_messages{_labels((('stream', stream),))} {depth}")

        return "\n".join(lines) + "\n"


_REGISTRY = ObservabilityRegistry()


def get_observability_registry() -> ObservabilityRegistry:
    """Process-wide registry used by the middleware, `/metrics` and the ops router."""
    return _REGISTRY


class ObservabilityMiddleware:
    """Pure-ASGI request correlation + latency/status metrics.

    Pure ASGI on purpose: `BaseHTTPMiddleware` wraps the downstream app in a
    task that buffers `http.response.body` messages, which breaks SSE endpoints
    such as `/api/v1/inbox/stream`. Here every message is forwarded untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: ObservabilityRegistry | None = None,
        telemetry: TelemetryConfig | None = None,
    ) -> None:
        self.app = app
        self.registry = registry or _REGISTRY
        self.telemetry = telemetry or TelemetryConfig()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = extract_request_id(scope)
        state = scope.get("state")
        if not isinstance(state, dict):
            state = {}
            scope["state"] = state
        state["request_id"] = request_id
        started = time.perf_counter()
        status_code = 500

        async def send_with_correlation(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except Exception as exc:
            dispatch_error_event(
                config=self.telemetry,
                message=f"{type(exc).__name__}: {exc}",
                request_id=request_id,
                path=str(scope.get("path") or ""),
                method=str(scope.get("method") or ""),
                status_code=500,
                exc_type=type(exc).__name__,
            )
            raise
        finally:
            self.registry.observe_request(
                method=str(scope.get("method") or "GET"),
                path_template=route_path_template(scope),
                status=status_code,
                duration_seconds=time.perf_counter() - started,
            )


# Backwards-compatible explicit name used by the Track 8 wiring notes.
RequestCorrelationMetricsMiddleware = ObservabilityMiddleware


metrics_router = APIRouter(prefix="/metrics", tags=["ops"])


@metrics_router.get("", include_in_schema=True)
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus/OpenMetrics scrape target for the in-process registry."""
    return PlainTextResponse(
        get_observability_registry().render(),
        media_type=PROMETHEUS_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Optional Sentry / OTLP telemetry (strictly best effort, strictly opt-in)
# ---------------------------------------------------------------------------

TelemetryDeliverer = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[bool]]


def _sentry_target(dsn: str) -> tuple[str, str] | None:
    """Parse a Sentry DSN into `(store_url, public_key)`; `None` when malformed."""
    parsed = urlparse(dsn)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    public_key = parsed.username or ""
    project_id = parsed.path.strip("/")
    if not public_key or not project_id.isdigit():
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}/api/{project_id}/store/", public_key


def _otlp_traces_target(endpoint: str) -> str | None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return endpoint.rstrip("/") + "/v1/traces"


@dataclass(frozen=True)
class TelemetryConfig:
    """Opt-in telemetry wiring derived from settings; disabled when both are empty."""

    sentry_dsn: str = ""
    otlp_endpoint: str = ""
    service_name: str = SERVICE_NAME
    timeout_seconds: float = 2.0
    environment: str = "development"

    @property
    def sentry(self) -> tuple[str, str] | None:
        return _sentry_target(self.sentry_dsn.strip()) if self.sentry_dsn.strip() else None

    @property
    def otlp_traces_url(self) -> str | None:
        return (
            _otlp_traces_target(self.otlp_endpoint.strip())
            if self.otlp_endpoint.strip()
            else None
        )

    @property
    def enabled(self) -> bool:
        return self.sentry is not None or self.otlp_traces_url is not None

    @classmethod
    def from_settings(cls, settings: Any) -> TelemetryConfig:
        return cls(
            sentry_dsn=getattr(settings, "SENTRY_DSN", "") or "",
            otlp_endpoint=getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "") or "",
            environment=getattr(settings, "APP_ENV", "development") or "development",
        )


async def _http_deliver(target: str, payload: dict[str, Any], headers: dict[str, str]) -> bool:
    """POST one telemetry document; any failure is a silent `False`."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.post(target, json=payload, headers=headers)
            return response.status_code < 400
    except Exception:  # noqa: BLE001 - telemetry must never break the request path
        return False


async def _deliver_once(
    deliverer: TelemetryDeliverer,
    target: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> bool:
    try:
        result: Any = deliverer(target, payload, headers)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except BaseException:  # noqa: BLE001 - never propagate telemetry failure
        return False


def _schedule(coroutine: Awaitable[Any]) -> None:
    """Run a delivery coroutine in the background, swallowing every outcome."""
    guarded = _deliver_guarded(coroutine)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(guarded)
        task.add_done_callback(_discard_task_result)
        return
    threading.Thread(target=lambda: asyncio.run(guarded), daemon=True).start()


async def _deliver_guarded(coroutine: Awaitable[Any]) -> None:
    try:
        await coroutine
    except BaseException:  # noqa: BLE001 - background delivery must stay silent
        return


def _discard_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    task.exception()


def _sentry_headers(public_key: str, config: TelemetryConfig) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Sentry-Auth": (
            "Sentry sentry_version=7, sentry_key="
            f"{public_key}, sentry_client={config.service_name}/0.1.0"
        ),
    }


def dispatch_error_event(
    *,
    config: TelemetryConfig,
    message: str,
    request_id: str | None = None,
    path: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    exc_type: str | None = None,
    deliverer: TelemetryDeliverer | None = None,
) -> bool:
    """Schedule one Sentry event; returns `True` only when it was actually queued."""
    sentry = config.sentry
    if sentry is None:
        return False
    target, public_key = sentry
    payload: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "message": message[:2000],
        "level": "error",
        "platform": "python",
        "logger": config.service_name,
        "environment": config.environment,
        "tags": {
            key: value
            for key, value in (
                ("request_id", request_id),
                ("path", path),
                ("method", method),
                ("status", None if status_code is None else str(status_code)),
                ("exception_type", exc_type),
            )
            if value
        },
        "extra": {
            "path": path or "",
            "method": method or "",
            "status_code": status_code,
            "request_id": request_id or "",
        },
        "timestamp": time.time(),
    }
    headers = _sentry_headers(public_key, config)
    try:
        _schedule(_deliver_once(deliverer or _http_deliver, target, payload, headers))
    except Exception:  # noqa: BLE001 - scheduling must never break a request
        return False
    return True


def dispatch_trace_span(
    *,
    config: TelemetryConfig,
    name: str,
    attributes: Mapping[str, Any] | None = None,
    deliverer: TelemetryDeliverer | None = None,
) -> bool:
    """Schedule one OTLP/HTTP trace span; returns `True` only when queued."""
    target = config.otlp_traces_url
    if target is None:
        return False
    span_attributes = [
        {"key": str(key), "value": {"stringValue": str(value)}}
        for key, value in (attributes or {}).items()
    ]
    payload: dict[str, Any] = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": config.service_name},
                        },
                        {
                            "key": "deployment.environment",
                            "value": {"stringValue": config.environment},
                        },
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": config.service_name},
                        "spans": [
                            {
                                "traceId": uuid.uuid4().hex,
                                "spanId": uuid.uuid4().hex[:16],
                                "name": name[:200],
                                "kind": 1,
                                "startTimeUnixNano": str(int(time.time() * 1_000_000_000)),
                                "endTimeUnixNano": str(int(time.time() * 1_000_000_000)),
                                "attributes": span_attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    headers = {"Content-Type": "application/json"}
    try:
        _schedule(_deliver_once(deliverer or _http_deliver, target, payload, headers))
    except Exception:  # noqa: BLE001
        return False
    return True


class TelemetryHook(Protocol):
    """Structural contract for tests that inject a fake collector."""

    async def __call__(
        self, target: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> bool: ...
