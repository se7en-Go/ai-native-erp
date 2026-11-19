"""Phase 3C growth service: collection, cleaning/grading, CRM sync and campaigns.

Compliance boundary, enforced here rather than in the UI:

* Collection only reads the three documented compliant sources (see
  ``collectors.py``); there is no Meta/WhatsApp scraper in this module.
* Cleaning is deterministic: E.164 phonenumbers, lowercase emails, host-only
  websites and a SHA-256 fingerprint. Nothing is inferred from a model.
* CRM sync is idempotent: a prospect can produce exactly one ``leads`` row.
* Campaign launch re-runs the same gates as the preview inside the transaction,
  so a stale UI preview can never authorize a send. WhatsApp gates are the
  global suppression list, the US ``+1`` marketing prohibition, a recorded
  opt-in, and the dual token bucket. Email carries RFC 8058 ``List-Unsubscribe``.
* No campaign path performs an outbound paid network call: launch records the
  per-recipient outcome and hands delivery to the channel worker.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
import phonenumbers
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.channels.rate_limiter import DualTokenBucketLimiter
from erp_core.crm.models import Lead
from erp_core.growth.collectors import (
    CollectedRecord,
    CollectRequest,
    build_collector,
    provider_unavailable,
    split_region,
)
from erp_core.growth.models import (
    CampaignRecipient,
    CollectedProspect,
    LeadCollectionJob,
    MarketingCampaign,
)
from erp_core.growth.schemas import (
    CampaignChannelType,
    CampaignCreate,
    CampaignLaunchResult,
    CampaignPreview,
    CampaignRead,
    CampaignRecipientRead,
    CampaignStatus,
    CampaignUpdate,
    CollectionSummary,
    LeadCollectionJobCreate,
    LeadCollectionJobRead,
    ProspectCleanResult,
    ProspectGrade,
    ProspectRead,
    SuppressionReason,
    SyncedLead,
    SyncToCrmResult,
)
from erp_core.identity.models import B2bCompany, Customer
from erp_core.shared.enums import ChannelType, CustomerType, LeadStatus
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import escape_like, page_of, record_not_found

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_MISSING_ADDRESS_PREFIX = "missing:"
_GRADE_SCORE = {"A": 90, "B": 60, "C": 30}

REASON_ADDRESS_MISSING = "地址缺失"
REASON_EMAIL_INVALID = "邮箱格式无效"
REASON_SUPPRESSED = "命中退订/抑制名单"
REASON_WHATSAPP_US_BAN = "WhatsApp 美国 +1 营销禁令"
REASON_WHATSAPP_NO_OPT_IN = "缺少 WhatsApp Opt-In 许可"
REASON_RATE_LIMITED = "超出 WhatsApp 发送速率"

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def conflict(detail: str, *, code: str, slug: str) -> AppError:
    return AppError(slug=slug, title="State Conflict", status_code=409, detail=detail, code=code)


def normalize_email(raw: str | None) -> str | None:
    """Lowercase, trim and validate; anything unparseable is dropped, never guessed."""
    candidate = (raw or "").strip().lower()
    return candidate if _EMAIL_RE.match(candidate) else None


def normalize_phone(raw: str | None, region_hint: str | None = None) -> str | None:
    """Return E.164 or ``None``. A local number without a region hint is never invented."""
    candidate = re.sub(r"[^\d+]", "", (raw or "").strip())
    if not candidate:
        return None
    hint: str | None = None
    if not candidate.startswith("+"):
        if region_hint is None or not re.fullmatch(r"[A-Za-z]{2}", region_hint.strip()):
            return None
        hint = region_hint.strip().upper()
    try:
        parsed = phonenumbers.parse(candidate, hint)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def website_domain(raw: str | None) -> str | None:
    """Host-only website key used for dedupe and for matching an existing B2B company."""
    candidate = (raw or "").strip().lower()
    if not candidate:
        return None
    candidate = re.sub(r"^[a-z][a-z0-9+.-]*://", "", candidate)
    host = re.split(r"[/?#]", candidate, maxsplit=1)[0]
    host = host.split("@")[-1].split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host and " " not in host else None


def dedupe_fingerprint(
    *,
    phone_e164: str | None,
    email: str | None,
    website: str | None,
    company_name: str,
    city: str | None,
    country: str | None,
) -> str:
    """Deterministic identity key: strongest available signal wins, in a fixed order."""
    domain = website_domain(website)
    if phone_e164:
        key = f"phone:{phone_e164}"
    elif email:
        key = f"email:{email}"
    elif domain:
        key = f"web:{domain}"
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", company_name.strip().lower()).strip("-")
        key = f"name:{slug}|{(city or '').strip().lower()}|{(country or '').strip().upper()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def grade_for(
    *,
    phone_e164: str | None,
    email: str | None,
    website: str | None,
    rating: Decimal | None,
) -> ProspectGrade:
    """Deterministic A/B/C grading over phone, email, website and rating only.

    score = 2*phone + 2*email + 1*website + (2 if rating >= 4.5 else 1 if rating >= 4.0)
    A: has a phone and score >= 4 (phone plus another strong signal)
    B: score >= 2 (any two weak signals, or one strong signal)
    C: everything else
    """
    score = 0
    if phone_e164:
        score += 2
    if email:
        score += 2
    if website_domain(website):
        score += 1
    if rating is not None:
        if rating >= Decimal("4.50"):
            score += 2
        elif rating >= Decimal("4.00"):
            score += 1
    if phone_e164 and score >= 4:
        return "A"
    if score >= 2:
        return "B"
    return "C"


def csv_safe_cell(value: object) -> str:
    """Neutralize spreadsheet formula injection before RFC 4180 quoting."""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


@dataclass(slots=True)
class ResolvedTarget:
    customer_id: UUID | None
    prospect_id: UUID | None
    email: str | None
    phone: str | None


class GrowthService:
    """Stateless coordinator; every dependency is injectable for deterministic tests."""

    def __init__(
        self,
        *,
        consent: ConsentAndSuppressionService | None = None,
        http_client: httpx.AsyncClient | None = None,
        google_places_api_key: str = "",
        overpass_min_interval_seconds: float = 1.0,
        rate_limiter: DualTokenBucketLimiter | None = None,
        unsubscribe_base_url: str = "https://erp.local/api/v1/growth/unsubscribe",
    ) -> None:
        self._consent = consent or ConsentAndSuppressionService()
        self._http_client = http_client
        self._google_places_api_key = google_places_api_key
        self._overpass_min_interval_seconds = overpass_min_interval_seconds
        self._rate_limiter = rate_limiter
        self._unsubscribe_base_url = unsubscribe_base_url

    # ------------------------------------------------------------------ jobs
    async def list_jobs(
        self, session: AsyncSession, tenant_id: UUID, limit: int, offset: int
    ) -> PaginatedResponse[LeadCollectionJobRead]:
        stmt = (
            sa.select(LeadCollectionJob)
            .where(LeadCollectionJob.tenant_id == tenant_id)
            .order_by(LeadCollectionJob.created_at.desc(), LeadCollectionJob.id)
        )
        return await page_of(session, stmt, LeadCollectionJobRead.model_validate, limit, offset)

    async def collection_summary(self, session: AsyncSession, tenant_id: UUID) -> CollectionSummary:
        prepared = await session.scalar(
            sa.select(sa.func.count())
            .select_from(CollectedProspect)
            .where(
                CollectedProspect.tenant_id == tenant_id,
                CollectedProspect.sync_status.in_(('cleaned', 'synced_to_crm')),
            )
        )
        synced = await session.scalar(
            sa.select(sa.func.count())
            .select_from(CollectedProspect)
            .where(
                CollectedProspect.tenant_id == tenant_id,
                CollectedProspect.sync_status == 'synced_to_crm',
            )
        )
        collected = await session.scalar(
            sa.select(sa.func.count())
            .select_from(CollectedProspect)
            .where(CollectedProspect.tenant_id == tenant_id)
        )
        jobs = await session.scalar(
            sa.select(sa.func.count())
            .select_from(LeadCollectionJob)
            .where(LeadCollectionJob.tenant_id == tenant_id)
        )
        return CollectionSummary(
            collected=int(collected or 0),
            cleaned=int(prepared or 0),
            synced=int(synced or 0),
            jobs=int(jobs or 0),
        )

    async def create_job(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: UUID,
        payload: LeadCollectionJobCreate,
    ) -> LeadCollectionJob:
        job = LeadCollectionJob(
            tenant_id=tenant_id,
            source_provider=payload.source_provider,
            keywords=list(payload.keywords),
            target_region=(payload.target_region or None),
            max_results=payload.max_results,
            min_rating=payload.min_rating,
            require_phone=payload.require_phone,
            status="pending",
            created_by=actor_id,
        )
        session.add(job)
        await session.flush()
        return job

    async def get_job(
        self, session: AsyncSession, tenant_id: UUID, job_id: UUID, *, lock: bool = False
    ) -> LeadCollectionJob:
        stmt = sa.select(LeadCollectionJob).where(
            LeadCollectionJob.tenant_id == tenant_id, LeadCollectionJob.id == job_id
        )
        if lock:
            stmt = stmt.with_for_update()
        job = await session.scalar(stmt)
        if job is None:
            raise record_not_found("Collection job")
        return job

    async def run_job(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        job_id: UUID,
        *,
        csv_content: str | None = None,
    ) -> LeadCollectionJob:
        """Execute a job once. A completed job is returned as-is (idempotent)."""
        job = await self.get_job(session, tenant_id, job_id, lock=True)
        if job.status == "completed":
            return job
        job.status = "running"
        job.error_message = None
        await session.flush()

        request = CollectRequest(
            keywords=tuple(job.keywords),
            target_region=job.target_region or "",
            max_results=job.max_results,
            min_rating=job.min_rating,
            require_phone=job.require_phone,
            csv_content=csv_content,
        )
        try:
            records = await self._collect(job.source_provider, request)
        except AppError as exc:
            job.status = "failed"
            job.error_message = exc.detail
            job.completed_at = datetime.now(UTC)
            await session.flush()
            raise
        filtered = [
            record
            for record in records
            if (not job.require_phone or bool(record.phone_e164))
            and (
                job.min_rating is None
                or (record.rating is not None and record.rating >= job.min_rating)
            )
        ][: job.max_results]
        inserted = await self._insert_prospects(session, tenant_id, job.id, filtered)
        job.collected_count = inserted
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        await session.flush()
        return job

    async def _collect(
        self, source_provider: str, request: CollectRequest
    ) -> list[CollectedRecord]:
        if self._http_client is not None:
            collector = build_collector(
                source_provider,
                request,
                client=self._http_client,
                google_places_api_key=self._google_places_api_key,
                overpass_min_interval_seconds=self._overpass_min_interval_seconds,
            )
            return await collector.collect()
        async with httpx.AsyncClient(timeout=30.0) as client:
            collector = build_collector(
                source_provider,
                request,
                client=client,
                google_places_api_key=self._google_places_api_key,
                overpass_min_interval_seconds=self._overpass_min_interval_seconds,
            )
            return await collector.collect()

    async def _insert_prospects(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        job_id: UUID,
        records: list[CollectedRecord],
    ) -> int:
        rows: list[dict[str, Any]] = []
        for record in records:
            phone = normalize_phone(record.phone_e164, record.country)
            email = normalize_email(record.email)
            fingerprint = dedupe_fingerprint(
                phone_e164=phone,
                email=email,
                website=record.website,
                company_name=record.company_name,
                city=record.city,
                country=record.country,
            )
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "company_name": record.company_name[:255],
                    "contact_name": record.contact_name,
                    "phone_e164": phone or record.phone_e164,
                    "email": email or record.email,
                    "website": record.website,
                    "country": (record.country or "")[:2].upper() or None,
                    "city": record.city,
                    "latitude": record.latitude,
                    "longitude": record.longitude,
                    "rating": record.rating,
                    "whatsapp_reachable": record.whatsapp_reachable,
                    "dedupe_fingerprint": fingerprint,
                    "sync_status": "raw",
                    "raw_metadata": record.raw_metadata,
                }
            )
        if not rows:
            return 0
        statement = (
            pg_insert(CollectedProspect)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_fingerprint"])
            .returning(CollectedProspect.id)
        )
        inserted = (await session.execute(statement)).scalars().all()
        return len(inserted)

    # -------------------------------------------------------------- prospects
    async def list_prospects(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        grade: str | None = None,
        sync_status: str | None = None,
        job_id: UUID | None = None,
        query: str | None = None,
    ) -> PaginatedResponse[ProspectRead]:
        stmt = sa.select(CollectedProspect).where(CollectedProspect.tenant_id == tenant_id)
        if grade is not None:
            stmt = stmt.where(CollectedProspect.grade == grade)
        if sync_status is not None:
            stmt = stmt.where(CollectedProspect.sync_status == sync_status)
        if job_id is not None:
            stmt = stmt.where(CollectedProspect.job_id == job_id)
        if query:
            needle = f"%{escape_like(query.strip())}%"
            stmt = stmt.where(
                sa.or_(
                    CollectedProspect.company_name.ilike(needle, escape="!"),
                    CollectedProspect.contact_name.ilike(needle, escape="!"),
                    CollectedProspect.email.ilike(needle, escape="!"),
                    CollectedProspect.phone_e164.ilike(needle, escape="!"),
                    CollectedProspect.city.ilike(needle, escape="!"),
                )
            )
        stmt = stmt.order_by(CollectedProspect.created_at.desc(), CollectedProspect.id)
        return await page_of(session, stmt, ProspectRead.model_validate, limit, offset)

    async def clean_prospects(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        prospect_ids: list[UUID] | None = None,
        *,
        limit: int = 500,
    ) -> ProspectCleanResult:
        """Normalize, dedupe and grade raw prospects deterministically."""
        stmt = (
            sa.select(CollectedProspect)
            .where(
                CollectedProspect.tenant_id == tenant_id,
                CollectedProspect.sync_status == "raw",
            )
            .order_by(CollectedProspect.created_at, CollectedProspect.id)
            .limit(limit)
            .with_for_update()
        )
        if prospect_ids is not None:
            stmt = stmt.where(CollectedProspect.id.in_(prospect_ids))
        rows = list(await session.scalars(stmt))
        graded: Counter[ProspectGrade] = Counter()
        cleaned = 0
        discarded = 0
        touched_jobs: set[UUID] = set()
        for row in rows:
            phone = normalize_phone(row.phone_e164, row.country)
            email = normalize_email(row.email)
            fingerprint = dedupe_fingerprint(
                phone_e164=phone,
                email=email,
                website=row.website,
                company_name=row.company_name,
                city=row.city,
                country=row.country,
            )
            duplicate = await session.scalar(
                sa.select(CollectedProspect.id)
                .where(
                    CollectedProspect.tenant_id == tenant_id,
                    CollectedProspect.dedupe_fingerprint == fingerprint,
                    CollectedProspect.id != row.id,
                )
                .limit(1)
            )
            if duplicate is not None:
                row.sync_status = "discarded"
                row.raw_metadata = {
                    **row.raw_metadata,
                    "discarded_reason": "duplicate_fingerprint",
                    "duplicate_of": str(duplicate),
                }
                discarded += 1
            else:
                row.phone_e164 = phone or row.phone_e164
                row.email = email or row.email
                row.dedupe_fingerprint = fingerprint
                row.grade = grade_for(
                    phone_e164=row.phone_e164,
                    email=row.email,
                    website=row.website,
                    rating=row.rating,
                )
                row.sync_status = "cleaned"
                graded[row.grade] += 1
                cleaned += 1
            if row.job_id is not None:
                touched_jobs.add(row.job_id)
            await session.flush()
        for job_id in touched_jobs:
            job = await session.scalar(
                sa.select(LeadCollectionJob).where(
                    LeadCollectionJob.tenant_id == tenant_id, LeadCollectionJob.id == job_id
                )
            )
            if job is None:
                continue
            job.cleaned_count = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(CollectedProspect)
                    .where(
                        CollectedProspect.tenant_id == tenant_id,
                        CollectedProspect.job_id == job_id,
                        CollectedProspect.sync_status.in_(("cleaned", "synced_to_crm")),
                    )
                )
                or 0
            )
        await session.flush()
        items = [ProspectRead.model_validate(row) for row in rows if row.sync_status == "cleaned"]
        return ProspectCleanResult(
            cleaned=cleaned,
            discarded_duplicates=discarded,
            graded={grade: int(graded.get(grade, 0)) for grade in ("A", "B", "C")},
            items=items,
        )

    async def sync_to_crm(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: UUID,
        prospect_ids: list[UUID],
    ) -> SyncToCrmResult:
        """Atomically materialize ``b2b_companies`` + ``customers`` + ``leads``."""
        rows = list(
            await session.scalars(
                sa.select(CollectedProspect)
                .where(
                    CollectedProspect.tenant_id == tenant_id,
                    CollectedProspect.id.in_(prospect_ids),
                )
                .order_by(CollectedProspect.created_at, CollectedProspect.id)
                .with_for_update()
            )
        )
        synced: list[SyncedLead] = []
        skipped = 0
        touched_jobs: set[UUID] = set()
        for row in rows:
            if row.sync_status not in ("cleaned", "synced_to_crm") or row.crm_lead_id is not None:
                skipped += 1
                continue
            company_id = await self._resolve_company(session, tenant_id, row)
            customer = await self._resolve_customer(session, tenant_id, row, company_id)
            requirement_note = (
                f"Collected from {row.raw_metadata.get('provider', 'compliant_source')} "
                f"({row.city or 'unknown city'}, {row.country or 'unknown country'}); "
                f"grade {row.grade}."
            )
            lead = Lead(
                tenant_id=tenant_id,
                customer_id=customer.id,
                source_channel="growth_map",
                utm_campaign=None,
                lead_type="b2b_inquiry",
                qualification_score=_GRADE_SCORE.get(row.grade, 30),
                extracted_requirements={
                    "prospect_id": str(row.id),
                    "company_name": row.company_name,
                    "grade": row.grade,
                    "city": row.city,
                    "country": row.country,
                    "rating": str(row.rating) if row.rating is not None else None,
                    "notes": requirement_note,
                },
                status=LeadStatus.NEW,
            )
            session.add(lead)
            await session.flush()
            row.sync_status = "synced_to_crm"
            row.crm_lead_id = lead.id
            synced.append(
                SyncedLead(
                    prospect_id=row.id,
                    crm_lead_id=lead.id,
                    customer_id=customer.id,
                    company_id=company_id,
                )
            )
            if row.job_id is not None:
                touched_jobs.add(row.job_id)
        for job_id in touched_jobs:
            job = await session.scalar(
                sa.select(LeadCollectionJob).where(
                    LeadCollectionJob.tenant_id == tenant_id, LeadCollectionJob.id == job_id
                )
            )
            if job is None:
                continue
            job.synced_count = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(CollectedProspect)
                    .where(
                        CollectedProspect.tenant_id == tenant_id,
                        CollectedProspect.job_id == job_id,
                        CollectedProspect.sync_status == "synced_to_crm",
                    )
                )
                or 0
            )
        await session.flush()
        return SyncToCrmResult(synced=len(synced), skipped=skipped, leads=synced)

    async def _resolve_company(
        self, session: AsyncSession, tenant_id: UUID, row: CollectedProspect
    ) -> UUID | None:
        domain = website_domain(row.website)
        stmt = sa.select(B2bCompany).where(
            B2bCompany.tenant_id == tenant_id, B2bCompany.legal_name == row.company_name
        )
        if domain is not None:
            stmt = sa.select(B2bCompany).where(
                B2bCompany.tenant_id == tenant_id, B2bCompany.website_domain == domain
            )
        existing = await session.scalar(stmt.limit(1))
        if existing is not None:
            return existing.id
        country = (row.country or "").strip().upper()
        company = B2bCompany(
            tenant_id=tenant_id,
            legal_name=row.company_name[:255],
            country_code=country if re.fullmatch(r"[A-Z]{2}", country) else "US",
            website_domain=domain,
        )
        session.add(company)
        await session.flush()
        return company.id

    async def _resolve_customer(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        row: CollectedProspect,
        company_id: UUID | None,
    ) -> Customer:
        stmt = sa.select(Customer).where(Customer.tenant_id == tenant_id)
        if row.email:
            stmt = stmt.where(sa.func.lower(Customer.primary_email) == row.email)
        elif row.phone_e164:
            stmt = stmt.where(Customer.primary_phone_e164 == row.phone_e164)
        else:
            stmt = stmt.where(
                Customer.company_id == company_id, Customer.display_name == row.company_name
            )
        existing = await session.scalar(stmt.limit(1))
        if existing is not None:
            return existing
        country = (row.country or "").strip().upper()
        customer = Customer(
            tenant_id=tenant_id,
            customer_type=(
                CustomerType.B2B_COMPANY_REP.value
                if row.contact_name
                else CustomerType.B2B_CONTACT.value
            ),
            company_id=company_id,
            display_name=(row.contact_name or row.company_name)[:255],
            primary_email=row.email,
            primary_phone_e164=row.phone_e164,
            country_code=country if re.fullmatch(r"[A-Z]{2}", country) else None,
            preferred_language="en",
            lifecycle_stage="lead",
        )
        session.add(customer)
        await session.flush()
        return customer

    async def export_prospects_csv(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        prospect_ids: list[UUID] | None = None,
        *,
        limit: int = 5000,
    ) -> str:
        stmt = (
            sa.select(CollectedProspect)
            .where(CollectedProspect.tenant_id == tenant_id)
            .order_by(CollectedProspect.created_at, CollectedProspect.id)
            .limit(limit)
        )
        if prospect_ids is not None:
            stmt = stmt.where(CollectedProspect.id.in_(prospect_ids))
        rows = list(await session.scalars(stmt))
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(
            [
                "company_name",
                "contact_name",
                "phone_e164",
                "email",
                "website",
                "country",
                "city",
                "latitude",
                "longitude",
                "rating",
                "grade",
                "sync_status",
                "whatsapp_reachable",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    csv_safe_cell(row.company_name),
                    csv_safe_cell(row.contact_name),
                    csv_safe_cell(row.phone_e164),
                    csv_safe_cell(row.email),
                    csv_safe_cell(row.website),
                    csv_safe_cell(row.country),
                    csv_safe_cell(row.city),
                    csv_safe_cell(row.latitude),
                    csv_safe_cell(row.longitude),
                    csv_safe_cell(str(row.rating) if row.rating is not None else ""),
                    csv_safe_cell(row.grade),
                    csv_safe_cell(row.sync_status),
                    csv_safe_cell(row.whatsapp_reachable),
                ]
            )
        return buffer.getvalue()

    # -------------------------------------------------------------- campaigns
    async def list_campaigns(
        self, session: AsyncSession, tenant_id: UUID, limit: int, offset: int
    ) -> PaginatedResponse[CampaignRead]:
        stmt = (
            sa.select(MarketingCampaign)
            .where(MarketingCampaign.tenant_id == tenant_id)
            .order_by(MarketingCampaign.created_at.desc(), MarketingCampaign.id)
        )
        return await page_of(session, stmt, CampaignRead.model_validate, limit, offset)

    async def get_campaign(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        campaign_id: UUID,
        *,
        lock: bool = False,
    ) -> MarketingCampaign:
        stmt = sa.select(MarketingCampaign).where(
            MarketingCampaign.tenant_id == tenant_id, MarketingCampaign.id == campaign_id
        )
        if lock:
            stmt = stmt.with_for_update()
        campaign = await session.scalar(stmt)
        if campaign is None:
            raise record_not_found("Marketing campaign")
        return campaign

    async def create_campaign(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: UUID,
        payload: CampaignCreate,
    ) -> MarketingCampaign:
        campaign = MarketingCampaign(
            tenant_id=tenant_id,
            name=payload.name,
            channel_type=payload.channel_type,
            subject=(payload.subject or None) if payload.channel_type == "email" else None,
            body_template=payload.body_template,
            status="draft",
            total_targets=0,
            created_by=actor_id,
        )
        session.add(campaign)
        await session.flush()
        recipients = await self._resolve_targets(session, tenant_id, payload.target_ids)
        created = await self._replace_recipients(
            session, tenant_id, campaign, payload.channel_type, recipients
        )
        campaign.total_targets = created
        await session.flush()
        return campaign

    async def update_campaign(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        campaign_id: UUID,
        payload: CampaignUpdate,
    ) -> MarketingCampaign:
        campaign = await self.get_campaign(session, tenant_id, campaign_id, lock=True)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("status") is not None:
            if campaign.status not in ("draft", "scheduled", "paused"):
                raise conflict(
                    "Only a draft, scheduled or paused campaign can change status.",
                    code="ERR_CAMPAIGN_STATE",
                    slug="campaign-state",
                )
            campaign.status = changes["status"]
        if campaign.status in ("running", "completed") and (
            changes.get("name") is not None
            or changes.get("subject") is not None
            or changes.get("body_template") is not None
            or changes.get("target_ids") is not None
        ):
            raise conflict(
                "A running or completed campaign can no longer be edited.",
                code="ERR_CAMPAIGN_STATE",
                slug="campaign-state",
            )
        if changes.get("name") is not None:
            campaign.name = changes["name"]
        if changes.get("subject") is not None:
            campaign.subject = changes["subject"]
        if changes.get("body_template") is not None:
            campaign.body_template = changes["body_template"]
        if changes.get("target_ids") is not None:
            recipients = await self._resolve_targets(session, tenant_id, changes["target_ids"])
            campaign.total_targets = await self._replace_recipients(
                session,
                tenant_id,
                campaign,
                cast(CampaignChannelType, campaign.channel_type),
                recipients,
            )
            campaign.sent_count = 0
            campaign.suppressed_count = 0
        campaign.updated_at = datetime.now(UTC)
        await session.flush()
        return campaign

    async def list_recipients(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        campaign_id: UUID,
        limit: int,
        offset: int,
    ) -> PaginatedResponse[CampaignRecipientRead]:
        await self.get_campaign(session, tenant_id, campaign_id)
        stmt = (
            sa.select(CampaignRecipient)
            .where(
                CampaignRecipient.tenant_id == tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
            )
            .order_by(CampaignRecipient.created_at, CampaignRecipient.id)
        )
        return await page_of(
            session, stmt, CampaignRecipientRead.model_validate, limit, offset
        )

    async def _resolve_targets(
        self, session: AsyncSession, tenant_id: UUID, target_ids: list[UUID]
    ) -> list[ResolvedTarget]:
        """Resolve customer/prospect IDs into channel addresses; never guesses one."""
        resolved: list[ResolvedTarget] = []
        customers = (
            await session.execute(
                sa.select(
                    Customer.id, Customer.primary_email, Customer.primary_phone_e164
                ).where(Customer.tenant_id == tenant_id, Customer.id.in_(target_ids))
            )
        ).all()
        for customer in customers:
            resolved.append(
                ResolvedTarget(
                    customer_id=customer.id,
                    prospect_id=None,
                    email=customer.primary_email,
                    phone=customer.primary_phone_e164,
                )
            )
        prospects = (
            await session.execute(
                sa.select(
                    CollectedProspect.id,
                    CollectedProspect.email,
                    CollectedProspect.phone_e164,
                ).where(
                    CollectedProspect.tenant_id == tenant_id,
                    CollectedProspect.id.in_(target_ids),
                )
            )
        ).all()
        for prospect in prospects:
            resolved.append(
                ResolvedTarget(
                    customer_id=None,
                    prospect_id=prospect.id,
                    email=prospect.email,
                    phone=prospect.phone_e164,
                )
            )
        return resolved

    def _missing_address(self, target: ResolvedTarget) -> str:
        return f"{_MISSING_ADDRESS_PREFIX}{target.customer_id or target.prospect_id}"

    def _address_for(self, channel: CampaignChannelType, target: ResolvedTarget) -> str:
        """Resolve the channel-appropriate address; an absent address is explicit, never guessed."""
        if channel == "email":
            email = normalize_email(target.email)
            return email or self._missing_address(target)
        normalized = (
            self._consent.normalize_address("whatsapp_cloud", target.phone)
            if target.phone
            else ""
        )
        digits = re.sub(r"[^\d+]", "", normalized)
        if digits.startswith("+") and len(digits) >= 8:
            return digits
        return self._missing_address(target)

    async def _replace_recipients(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        campaign: MarketingCampaign,
        channel: CampaignChannelType,
        targets: list[ResolvedTarget],
    ) -> int:
        await session.execute(
            sa.delete(CampaignRecipient).where(
                CampaignRecipient.tenant_id == tenant_id,
                CampaignRecipient.campaign_id == campaign.id,
            )
        )
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for target in targets:
            address = self._address_for(channel, target)
            if address in seen:
                continue
            seen.add(address)
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "campaign_id": campaign.id,
                    "customer_id": target.customer_id,
                    "prospect_id": target.prospect_id,
                    "address": address,
                    "status": "queued",
                }
            )
        if rows:
            await session.execute(pg_insert(CampaignRecipient).values(rows))
        await session.flush()
        return len(rows)

    async def _recipient_block_reason(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel: CampaignChannelType,
        recipient: CampaignRecipient,
    ) -> str | None:
        address = recipient.address
        if address.startswith(_MISSING_ADDRESS_PREFIX):
            return REASON_ADDRESS_MISSING
        if channel == "email":
            if not _EMAIL_RE.match(address):
                return REASON_EMAIL_INVALID
            if await self._consent.is_suppressed(session, tenant_id, "email", address):
                return REASON_SUPPRESSED
            return None
        if await self._consent.is_suppressed(session, tenant_id, "whatsapp_cloud", address):
            return REASON_SUPPRESSED
        if address.startswith("+1"):
            # Meta does not allow marketing templates to US (+1) numbers.
            return REASON_WHATSAPP_US_BAN
        if recipient.customer_id is None or not await self._consent.has_valid_opt_in(
            session, tenant_id, recipient.customer_id, "whatsapp_cloud"
        ):
            return REASON_WHATSAPP_NO_OPT_IN
        return None

    async def _queued_recipients(
        self, session: AsyncSession, tenant_id: UUID, campaign_id: UUID
    ) -> list[CampaignRecipient]:
        return list(
            await session.scalars(
                sa.select(CampaignRecipient)
                .where(
                    CampaignRecipient.tenant_id == tenant_id,
                    CampaignRecipient.campaign_id == campaign_id,
                    CampaignRecipient.status == "queued",
                )
                .order_by(CampaignRecipient.created_at, CampaignRecipient.id)
                .with_for_update()
            )
        )

    def _unsubscribe_header(self, campaign: MarketingCampaign) -> str:
        return (
            f"<{self._unsubscribe_base_url}?campaign={campaign.id}&token={{token}}>, "
            f"<mailto:unsubscribe@erp.local?subject=unsubscribe-{campaign.id}>"
        )

    async def preview_campaign(
        self, session: AsyncSession, tenant_id: UUID, campaign_id: UUID
    ) -> CampaignPreview:
        campaign = await self.get_campaign(session, tenant_id, campaign_id)
        recipients = await self._queued_recipients(session, tenant_id, campaign_id)
        reasons: Counter[str] = Counter()
        eligible = 0
        for recipient in recipients:
            reason = await self._recipient_block_reason(
                session,
                tenant_id,
                cast(CampaignChannelType, campaign.channel_type),
                recipient,
            )
            if reason is None:
                eligible += 1
            else:
                reasons[reason] += 1
        return CampaignPreview(
            campaign_id=campaign.id,
            channel_type=campaign.channel_type,
            eligible_count=eligible,
            suppressed_count=sum(reasons.values()),
            reasons=[
                SuppressionReason(reason=reason, count=count)
                for reason, count in sorted(reasons.items())
            ],
            total_targets=len(recipients),
            list_unsubscribe_header=(
                self._unsubscribe_header(campaign)
                if campaign.channel_type == "email"
                else None
            ),
            rate_limit_remaining=None,
        )

    async def launch_campaign(
        self, session: AsyncSession, tenant_id: UUID, campaign_id: UUID
    ) -> CampaignLaunchResult:
        campaign = await self.get_campaign(session, tenant_id, campaign_id, lock=True)
        if campaign.status not in ("draft", "scheduled", "paused"):
            raise conflict(
                "Only a draft, scheduled or paused campaign can be launched.",
                code="ERR_CAMPAIGN_NOT_LAUNCHABLE",
                slug="campaign-not-launchable",
            )
        recipients = await self._queued_recipients(session, tenant_id, campaign_id)
        reasons: Counter[str] = Counter()
        sent = 0
        suppressed = 0
        queued_remaining = 0
        channel = cast(CampaignChannelType, campaign.channel_type)
        for recipient in recipients:
            reason = await self._recipient_block_reason(
                session, tenant_id, channel, recipient
            )
            if reason is not None:
                recipient.status = "suppressed"
                recipient.error_reason = reason
                suppressed += 1
                reasons[reason] += 1
                continue
            if channel == "whatsapp_cloud" and self._rate_limiter is not None:
                allowed, _ = await self._rate_limiter.acquire(
                    ChannelType.WHATSAPP_CLOUD, str(tenant_id), recipient_id=recipient.address
                )
                if not allowed:
                    recipient.error_reason = REASON_RATE_LIMITED
                    queued_remaining += 1
                    reasons[REASON_RATE_LIMITED] += 1
                    continue
            recipient.status = "sent"
            recipient.sent_at = datetime.now(UTC)
            recipient.error_reason = None
            sent += 1
        campaign.sent_count = sent
        campaign.suppressed_count = suppressed
        campaign.status = "paused" if queued_remaining else "completed"
        campaign.updated_at = datetime.now(UTC)
        await session.flush()
        return CampaignLaunchResult(
            campaign_id=campaign.id,
            status=cast(CampaignStatus, campaign.status),
            sent=sent,
            suppressed=suppressed,
            queued_remaining=queued_remaining,
            reasons=[
                SuppressionReason(reason=reason, count=count)
                for reason, count in sorted(reasons.items())
            ],
        )


__all__ = [
    "GrowthService",
    "csv_safe_cell",
    "dedupe_fingerprint",
    "grade_for",
    "normalize_email",
    "normalize_phone",
    "provider_unavailable",
    "split_region",
    "website_domain",
]
