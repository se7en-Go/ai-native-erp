from __future__ import annotations

import asyncio
import base64
import binascii
from ipaddress import ip_address
from typing import Annotated, Any

import dns.asyncresolver
import dns.exception
import dns.name
import dns.rdtypes.ANY.TXT
import dns.resolver
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key
from fastapi import APIRouter, Depends
from pydantic import BaseModel, StringConstraints

from erp_core.shared.config import get_settings
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])
Host = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=200,
        pattern=r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)+\.?$",
    ),
]


class VerifyEmailDnsRequest(BaseModel):
    domain: Host
    dkim_selector: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,63}$")] = "resend"


class EmailDnsRead(BaseModel):
    spf_verified: bool
    dkim_verified: bool
    return_path_verified: bool


def get_onboarding_dns_resolver() -> dns.asyncresolver.Resolver:
    settings = get_settings()
    if not settings.ONBOARDING_DNS_NAMESERVER:
        return dns.asyncresolver.Resolver()
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [str(ip_address(settings.ONBOARDING_DNS_NAMESERVER))]
    resolver.port = settings.ONBOARDING_DNS_PORT
    return resolver


async def _resolve(
    resolver: dns.asyncresolver.Resolver,
    name: str,
    record_type: str,
) -> Any:
    try:
        return await resolver.resolve(name, record_type, search=False, lifetime=3)
    except dns.exception.DNSException:
        return ()


def _valid_dkim(record: str) -> bool:
    tags = dict(part.strip().split("=", 1) for part in record.split(";") if "=" in part)
    if tags.get("v", "DKIM1") != "DKIM1" or tags.get("k", "rsa") != "rsa":
        return False
    try:
        key = load_der_public_key(
            base64.b64decode("".join(tags.get("p", "").split()), validate=True)
        )
    except (ValueError, binascii.Error):
        return False
    return isinstance(key, RSAPublicKey) and key.key_size >= 2048


@router.post("/email/verify-dns")
async def verify_email_dns(
    body: VerifyEmailDnsRequest,
    _principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))],
    resolver: Annotated[dns.asyncresolver.Resolver, Depends(get_onboarding_dns_resolver)],
) -> EmailDnsRead:
    domain = body.domain.lower().rstrip(".") + "."
    spf, dkim, mx = await asyncio.gather(
        _resolve(resolver, domain, "TXT"),
        _resolve(resolver, f"{body.dkim_selector}._domainkey.{domain}", "TXT"),
        _resolve(resolver, f"bounce.{domain}", "MX"),
    )
    spf_records = [b"".join(record.strings).split() for record in spf]
    policies = [parts for parts in spf_records if parts and parts[0].lower() == b"v=spf1"]
    dkim_records = [b"".join(record.strings).decode("ascii", errors="replace") for record in dkim]
    return EmailDnsRead(
        spf_verified=len(policies) == 1 and len(policies[0]) > 1,
        dkim_verified=len(dkim_records) == 1 and _valid_dkim(dkim_records[0]),
        return_path_verified=any(record.exchange != dns.name.root for record in mx),
    )
