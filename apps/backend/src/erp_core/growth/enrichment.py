"""Compliant public-website business-email enrichment.

Design rules (they are deliberately strict, because this module fetches a URL that
a third party supplied):

* **Public pages only.** Only ``http``/``https`` home pages and their ``/contact``
  page are read. There is no crawling, no login, no form submission and no
  newsletter signup — the e-mail we keep is one the business already publishes.
* **SSRF guard.** Literal private/loopback/link-local/reserved addresses and
  internal hostnames are rejected before any request, DNS answers are re-checked
  when a resolver is available, and redirects are followed manually so every hop
  is validated again.
* **Bounded work.** 6 s timeout, at most 256 KiB of HTML per page, at most two
  pages and three redirect hops per prospect.
* **Deterministic extraction.** ``mailto:`` links win over plain-text matches, then
  role inboxes are ranked ``sales`` > ``info`` > ``purchasing`` > ``contact`` >
  ``export``. Nothing is guessed or pattern-generated.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from erp_core.growth.collectors import (
    COLLECTOR_USER_AGENT,
    WEBSITE_FETCH_TIMEOUT_SECONDS,
    WEBSITE_MAX_HTML_BYTES,
)

__all__ = [
    "EmailEnrichmentOutcome",
    "WebsiteEmailEnricher",
    "extract_public_emails",
    "is_blocked_host",
    "normalize_website_url",
    "system_dns_resolver",
]

_CONTACT_PATHS = ("/contact",)
_MAX_REDIRECTS = 3
_HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_MAILTO_RE = re.compile(r"mailto:([^\"'<>\s?#]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Role inboxes we rank first; everything else is still accepted.
_PRIORITY_LOCAL_PARTS = ("sales", "info", "purchasing", "contact", "export")
_PLACEHOLDER_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "domain.com",
        "yourdomain.com",
        "company.com",
        "placeholder.com",
        "email.com",
        "sentry.io",
        "sentry-next.wixpress.com",
        "wixpress.com",
        "sentry.wixpress.com",
    }
)
_PLACEHOLDER_LOCAL_PARTS = frozenset(
    {"you", "your", "yourname", "name", "email", "user", "username", "someone", "test", "john"}
)
_ASSET_TLDS = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "css", "js", "woff", "woff2"}
)
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".invalid")
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

DnsResolver = Callable[[str], Awaitable[Sequence[str]]]


async def system_dns_resolver(host: str) -> Sequence[str]:
    """Resolve a hostname through the platform stub resolver (``[]`` when unknown)."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return ()
    return tuple({str(info[4][0]) for info in infos})


def normalize_website_url(raw: str | None) -> str | None:
    """Return an absolute ``http(s)`` URL or ``None``; anything else is refused."""
    candidate = (raw or "").strip()
    if not candidate:
        return None
    if not _SCHEME_RE.match(candidate):
        candidate = f"https://{candidate}"
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, "")
    )


def _is_blocked_ip(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw.strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in _SHARED_ADDRESS_SPACE


def is_blocked_host(host: str | None) -> bool:
    """True when the host is (or looks like) anything other than a public endpoint."""
    if not host:
        return True
    candidate = host.strip().strip("[]").rstrip(".").lower()
    if not candidate:
        return True
    if candidate == "localhost" or candidate.endswith(_BLOCKED_HOST_SUFFIXES):
        return True
    if candidate.isdigit() and "." not in candidate:
        # A bare integer is an alternative IPv4 spelling (e.g. http://2130706433/).
        return True
    # Bare-hex / dotted-hex aliases resolve to integers too; treat them as unsafe.
    if re.fullmatch(r"0x[0-9a-f]+", candidate):
        return True
    return _is_blocked_ip(candidate)


def _is_placeholder(email: str) -> bool:
    local, _, domain = email.partition("@")
    if domain in _PLACEHOLDER_DOMAINS or domain.endswith(".example"):
        return True
    if domain.rsplit(".", 1)[-1].lower() in _ASSET_TLDS:
        return True
    if local in _PLACEHOLDER_LOCAL_PARTS and domain in _PLACEHOLDER_DOMAINS:
        return True
    return bool(re.fullmatch(r"[0-9a-f]{16,}", local))


def _rank(email: str) -> tuple[int, str]:
    local = email.split("@", 1)[0]
    for index, prefix in enumerate(_PRIORITY_LOCAL_PARTS):
        if local == prefix or local.startswith(f"{prefix}.") or local.startswith(f"{prefix}-"):
            return (index, email)
    return (len(_PRIORITY_LOCAL_PARTS), email)


def extract_public_emails(html: str) -> list[str]:
    """Extract rank-ordered, de-duplicated public business e-mails from one page."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _MAILTO_RE.finditer(html):
        candidate = unquote(match.group(1)).strip().lower()
        candidate = candidate.split("?", 1)[0].strip(" .,;:<>")
        if _EMAIL_RE.fullmatch(candidate) and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    for match in _EMAIL_RE.finditer(html):
        candidate = match.group(0).strip(" .,;:<>'\"").lower()
        if candidate in seen or not _EMAIL_RE.fullmatch(candidate):
            continue
        seen.add(candidate)
        found.append(candidate)
    cleaned = [email for email in found if not _is_placeholder(email)]
    return sorted(cleaned, key=_rank)


@dataclass(slots=True)
class EmailEnrichmentOutcome:
    """What one prospect lookup produced (never raises for a normal failure)."""

    emails: list[str] = field(default_factory=list)
    pages_checked: int = 0
    blocked: bool = False
    error: str | None = None

    @property
    def best(self) -> str | None:
        return self.emails[0] if self.emails else None


class WebsiteEmailEnricher:
    """Fetch a public home page (+ ``/contact``) and harvest published e-mails."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = WEBSITE_FETCH_TIMEOUT_SECONDS,
        max_html_bytes: int = WEBSITE_MAX_HTML_BYTES,
        user_agent: str = COLLECTOR_USER_AGENT,
        resolver: DnsResolver | None = system_dns_resolver,
    ) -> None:
        self._transport = transport
        self._timeout = timeout
        self._max_html_bytes = max_html_bytes
        self._user_agent = user_agent
        self._resolver = resolver

    async def find_emails(self, website: str | None) -> EmailEnrichmentOutcome:
        url = normalize_website_url(website)
        if url is None:
            return EmailEnrichmentOutcome(error="invalid_website")
        origin = self._origin(url)
        targets = [url]
        for path in _CONTACT_PATHS:
            contact = urljoin(origin + "/", path.lstrip("/"))
            if contact not in targets:
                targets.append(contact)
        outcome = EmailEnrichmentOutcome()
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        ) as client:
            for target in targets:
                if not await self._allowed(target):
                    outcome.blocked = True
                    continue
                html = await self._fetch(client, target, outcome)
                if not html:
                    continue
                outcome.pages_checked += 1
                found = extract_public_emails(html)
                if found:
                    outcome.emails = found
                    return outcome
        return outcome

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = parts.hostname
        if is_blocked_host(host):
            return False
        if self._resolver is None or host is None:
            return True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return True
        addresses = await self._resolver(host)
        if not addresses:
            # Unresolvable names cannot be reached; do not pretend they are safe.
            return False
        return not any(_is_blocked_ip(address) for address in addresses)

    async def _fetch(
        self, client: httpx.AsyncClient, url: str, outcome: EmailEnrichmentOutcome
    ) -> str | None:
        target = url
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                response = await client.get(
                    target,
                    headers={"User-Agent": self._user_agent, "Accept": _HTML_ACCEPT},
                )
            except httpx.HTTPError as exc:
                outcome.error = f"{type(exc).__name__}"
                return None
            if response.is_redirect:
                location = response.headers.get("location", "")
                redirect = normalize_website_url(urljoin(target, location))
                if redirect is None or not await self._allowed(redirect):
                    outcome.blocked = True
                    return None
                target = redirect
                continue
            if response.status_code >= 400:
                outcome.error = f"HTTP_{response.status_code}"
                return None
            content_type = response.headers.get("content-type", "")
            if content_type and "html" not in content_type.lower():
                return None
            body = response.content[: self._max_html_bytes]
            charset = response.charset_encoding or "utf-8"
            return body.decode(charset, errors="ignore")
        outcome.error = "too_many_redirects"
        return None
