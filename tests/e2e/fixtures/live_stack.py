"""Supervise the real local stack for Playwright. Docker services are caller-owned."""

from __future__ import annotations

import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from provider_http import ProviderServer

from erp_core.shared.config import get_settings
from erp_core.shared.storage import ObjectStorageService

ROOT = Path(__file__).resolve().parents[3]
API = "http://127.0.0.1:8000"


def dns_server() -> socketserver.UDPServer:
    from base64 import b64encode

    domain = "mail.qzled.example.com."
    public_key = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    dkim = "v=DKIM1; k=rsa; p=" + b64encode(der).decode("ascii")
    records = {
        (domain, dns.rdatatype.TXT): '"v=spf1 mx -all"',
        (f"resend._domainkey.{domain}", dns.rdatatype.TXT): f'"{dkim[:200]}" "{dkim[200:]}"',
        (f"bounce.{domain}", dns.rdatatype.MX): f"10 mx.{domain}",
    }

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            packet, sock = self.request
            query = dns.message.from_wire(packet)
            response = dns.message.make_response(query)
            (question,) = query.question
            record = records.get((str(question.name).lower(), question.rdtype))
            if record:
                response.answer.append(
                    dns.rrset.from_text(
                        question.name,
                        60,
                        "IN",
                        question.rdtype,
                        record,
                    )
                )
            else:
                response.set_rcode(dns.rcode.NXDOMAIN)
            sock.sendto(response.to_wire(), self.client_address)

    return socketserver.UDPServer(("127.0.0.1", 0), Handler)


def terminate(proc: subprocess.Popen) -> None:
    # Next and its compiler may have descendants. Each owned child has its own process group.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def wait_for_ready(proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"FastAPI exited before readiness: {proc.returncode}")
        try:
            with urlopen(f"{API}/readyz", timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError("FastAPI /readyz did not confirm PostgreSQL and Redis readiness")


def set_env(key: str, value: str) -> None:
    if not os.environ.get(key):
        os.environ[key] = value


def main() -> None:
    import asyncio

    if os.environ.get("E2E_MODE") != "live":
        raise RuntimeError("This launcher is only for E2E_MODE=live")
    slug = os.environ["E2E_RUN_SLUG"]
    defaults = {
        "DATABASE_MIGRATOR_URL": "postgresql+asyncpg://app_migrator:migrator_dev_pass@127.0.0.1:5433/erp_saas",
        "DATABASE_RUNTIME_URL": "postgresql+asyncpg://app_runtime:runtime_dev_pass@127.0.0.1:6432/erp_saas",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "RESEND_WEBHOOK_SECRET": "dev-resend-secret",
        "SHOPIFY_CLIENT_SECRET": "dev-shopify-secret",
        "TRACK17_WEBHOOK_SECRET": "dev-17track-secret",
    }
    for key, value in defaults.items():
        set_env(key, value)
    # Make the local provider selection deterministic; an explicit OpenAI key/base URL still wins.
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ[key] = ""
    os.environ.update(
        NEXT_PUBLIC_USE_MOCK_API="false", BFF_BACKEND_URL=API, NEXT_TELEMETRY_DISABLED="1"
    )

    with ExitStack() as stack:

        def start(command: list[str]) -> subprocess.Popen:
            proc = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
            stack.callback(terminate, proc)
            return proc

        for server in (ProviderServer(), dns_server()):
            stack.enter_context(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            port = str(server.server_address[1])
            if isinstance(server, ProviderServer):
                os.environ["E2E_PROVIDER_PORT"] = port
                if not os.environ.get("OPENAI_API_KEY"):
                    os.environ.update(
                        OPENAI_API_KEY="live-e2e-fixture-only",
                        OPENAI_BASE_URL=f"http://127.0.0.1:{port}/v1",
                    )
            elif not os.environ.get("ONBOARDING_DNS_NAMESERVER"):
                os.environ.update(ONBOARDING_DNS_NAMESERVER="127.0.0.1", ONBOARDING_DNS_PORT=port)
        asyncio.run(ObjectStorageService(get_settings()).ensure_bucket_exists())
        seed = start(
            [
                sys.executable,
                "apps/backend/scripts/seed_hybrid_factory.py",
                "--live-slug",
                slug,
            ]
        )
        if seed.wait() != 0:
            raise RuntimeError(f"Live workspace seed failed: {seed.returncode}")
        api = start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "erp_core.api_main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ]
        )
        worker = start([sys.executable, "tests/e2e/fixtures/live_worker.py"])
        wait_for_ready(api)
        web = start(
            [
                "node",
                "apps/web/node_modules/next/dist/bin/next",
                "dev",
                "apps/web",
                "--hostname",
                "127.0.0.1",
                "--port",
                "3000",
            ]
        )
        while True:
            for name, proc in (("API", api), ("worker", worker), ("Next.js", web)):
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} exited unexpectedly: {proc.returncode}")
            time.sleep(0.25)


if __name__ == "__main__":

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    main()
