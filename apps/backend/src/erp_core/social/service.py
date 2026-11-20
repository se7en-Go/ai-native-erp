"""Phase 3D social marketing service: accounts, calendar posts, mentions and CRM leads.

Compliance contract enforced here (not only in the UI):

* scheduling a post on a `manual_export` platform is rejected with 422
  `ERR_SOCIAL_MANUAL_PUBLISH_ONLY` — no silent "scheduled but never posted" rows;
* publishing never performs an outbound post: an API platform needs a *connected*
  account before the row may be marked published, and a manual platform needs an
  explicit human confirmation from the official console;
* OAuth tokens only ever reach the database through `EnvelopeCryptoService` with
  `AAD = f"{tenant_id}:{account_id}:social_oauth"`, and disconnect shreds the
  envelope so a revoked grant leaves no ciphertext behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.crm.models import Lead
from erp_core.identity.models import Customer
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import LeadStatus
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import page_of, record_not_found
from erp_core.social.marketing_playbooks import marketing_playbooks
from erp_core.social.models import SocialAccount, SocialMention, SocialPost
from erp_core.social.playbooks import PLAYBOOKS, playbook_for, supports_scheduled_publish
from erp_core.social.schemas import (
    AccountConnect,
    AccountRead,
    ConvertLeadResult,
    MentionCreate,
    MentionRead,
    PlatformRead,
    PostCreate,
    PostPublish,
    PostRead,
    PostUpdate,
    SocialPlaybookItem,
    SocialSummary,
)

SOCIAL_CRED_TYPE = "social_oauth"
_LEAD_SCORE = {"positive": 55, "neutral": 45, "negative": 30, "lead_intent": 70}


def manual_publish_only(platform: str) -> AppError:
    label = playbook_for(platform).label
    return AppError(
        slug="social-manual-publish-only",
        title="Manual Publishing Required",
        status_code=422,
        detail=(
            f"{label} exposes no third-party publishing API. Export the finished assets and "
            "publish them in the official creation console, then confirm the publish."
        ),
        code="ERR_SOCIAL_MANUAL_PUBLISH_ONLY",
    )


def schedule_time_required() -> AppError:
    return AppError(
        slug="social-schedule-time-required",
        title="Schedule Time Required",
        status_code=422,
        detail="A scheduled post needs an explicit scheduled_at timestamp.",
        code="ERR_SOCIAL_SCHEDULE_TIME_REQUIRED",
    )


def account_not_connected(platform: str) -> AppError:
    return AppError(
        slug="social-account-not-connected",
        title="Social Account Not Connected",
        status_code=409,
        detail=(
            f"No connected {playbook_for(platform).label} account exists in this workspace. "
            "Authorize one before recording a publish."
        ),
        code="ERR_SOCIAL_ACCOUNT_NOT_CONNECTED",
    )


def caption_provider_unavailable() -> AppError:
    """Fail closed: never fabricate marketing copy when no provider is configured."""
    return AppError(
        slug="ai-provider-unavailable",
        title="AI Provider Unavailable",
        status_code=503,
        detail="No LLM provider key is configured for this workspace.",
        code="ERR_AI_PROVIDER_UNAVAILABLE",
    )


def invalid_post_status(status: str) -> AppError:
    return AppError(
        slug="social-invalid-post-status",
        title="Invalid Post Status Transition",
        status_code=422,
        detail=(
            f"Status '{status}' cannot be set through this endpoint; only draft and scheduled "
            "are editable (publishing has its own endpoint)."
        ),
        code="ERR_INVALID_POST_STATUS",
    )


class SocialService:
    """Stateless service; the crypto service is the only collaborator it needs."""

    def __init__(self, crypto_service: EnvelopeCryptoService) -> None:
        self._crypto = crypto_service

    # -- platform matrix ---------------------------------------------------

    def platforms(self) -> list[PlatformRead]:
        return [
            PlatformRead(
                platform=item.platform,
                region=item.region,
                label=item.label,
                emoji=item.emoji,
                audience=item.audience,
                acquisition_plays=list(item.acquisition_plays),
                best_practices=list(item.best_practices),
                content_formats=list(item.content_formats),
                publish_mode=item.publish_mode,
                supports_scheduled_publish=item.publish_mode == "api",
                compliance_note=item.compliance_note,
            )
            for item in PLAYBOOKS
        ]

    def playbooks(self, region: str = "all") -> list[SocialPlaybookItem]:
        """Bilingual operating cards; the API claim is derived from the locked publish mode."""
        return [
            SocialPlaybookItem(
                platform=item.platform,
                region=item.region,
                name_zh=item.name_zh,
                name_en=item.name_en,
                category_zh=item.category_zh,
                category_en=item.category_en,
                audience_zh=item.audience_zh,
                audience_en=item.audience_en,
                lead_funnel_zh=item.lead_funnel_zh,
                lead_funnel_en=item.lead_funnel_en,
                best_practices_zh=list(item.best_practices_zh),
                best_practices_en=list(item.best_practices_en),
                official_api_supported=supports_scheduled_publish(item.platform),
                compliance_note_zh=item.compliance_note_zh,
                compliance_note_en=item.compliance_note_en,
            )
            for item in marketing_playbooks(region)
        ]

    # -- summary -----------------------------------------------------------

    async def summary(self, session: AsyncSession, tenant_id: UUID) -> SocialSummary:
        async def count(
            model: type[SocialPost] | type[SocialMention] | type[SocialAccount],
            *filters: sa.ColumnElement[bool],
        ) -> int:
            return int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(model)
                    .where(model.tenant_id == tenant_id, *filters)
                )
                or 0
            )

        return SocialSummary(
            total_posts=await count(SocialPost),
            published_posts=await count(SocialPost, SocialPost.status == "published"),
            scheduled_posts=await count(SocialPost, SocialPost.status == "scheduled"),
            draft_posts=await count(SocialPost, SocialPost.status == "draft"),
            social_leads=await count(SocialMention, SocialMention.sentiment == "lead_intent"),
            converted_leads=await count(
                SocialMention, SocialMention.converted_lead_id.is_not(None)
            ),
            mentions_total=await count(SocialMention),
            connected_accounts=await count(SocialAccount, SocialAccount.status == "connected"),
            supported_platforms=len(PLAYBOOKS),
        )

    # -- accounts ----------------------------------------------------------

    async def list_accounts(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        platform: str | None = None,
        status: str | None = None,
    ) -> PaginatedResponse[AccountRead]:
        query = sa.select(SocialAccount).where(SocialAccount.tenant_id == tenant_id)
        if platform:
            query = query.where(SocialAccount.platform == platform)
        if status:
            query = query.where(SocialAccount.status == status)
        return await page_of(
            session,
            query.order_by(SocialAccount.created_at, SocialAccount.id),
            self._account_read,
            limit,
            offset,
        )

    @staticmethod
    def _account_read(row: SocialAccount) -> AccountRead:
        return AccountRead(
            id=row.id,
            platform=row.platform,
            region=row.region,
            handle=row.handle,
            display_name=row.display_name,
            external_account_id=row.external_account_id,
            status=row.status,
            followers_count=row.followers_count,
            config=row.config or {},
            has_stored_token=row.token_ciphertext is not None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def connect_account(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: UUID,
        payload: AccountConnect,
    ) -> tuple[AccountRead, bool]:
        """Upsert the authorization for `(platform, handle)`; returns (row, created)."""
        playbook = playbook_for(payload.platform)
        existing = await session.scalar(
            sa.select(SocialAccount)
            .where(
                SocialAccount.tenant_id == tenant_id,
                SocialAccount.platform == payload.platform,
                SocialAccount.handle == payload.handle,
            )
            .with_for_update()
        )
        account = existing or SocialAccount(
            id=uuid4(),
            tenant_id=tenant_id,
            platform=payload.platform,
            region=playbook.region,
            handle=payload.handle,
            display_name=payload.display_name,
            created_by=actor_id,
        )
        account.region = playbook.region
        account.display_name = payload.display_name
        account.external_account_id = payload.external_account_id
        account.followers_count = payload.followers_count
        account.config = payload.config
        account.status = "connected"
        account.updated_at = datetime.now(UTC)
        if payload.access_token is not None:
            ciphertext, nonce, auth_tag, key_version = await self._crypto.encrypt_secret(
                session, tenant_id, account.id, SOCIAL_CRED_TYPE, payload.access_token
            )
            account.token_ciphertext = ciphertext
            account.token_nonce = nonce
            account.token_auth_tag = auth_tag
            account.token_key_version = key_version
        if existing is None:
            session.add(account)
        await session.flush()
        return self._account_read(account), existing is None

    async def disconnect_account(
        self, session: AsyncSession, tenant_id: UUID, account_id: UUID
    ) -> AccountRead:
        account = await session.scalar(
            sa.select(SocialAccount)
            .where(SocialAccount.tenant_id == tenant_id, SocialAccount.id == account_id)
            .with_for_update()
        )
        if account is None:
            raise record_not_found("Social account")
        account.status = "disconnected"
        # A revoked grant must not leave a decryptable token behind.
        account.token_ciphertext = None
        account.token_nonce = None
        account.token_auth_tag = None
        account.token_key_version = None
        account.updated_at = datetime.now(UTC)
        await session.flush()
        return self._account_read(account)

    # -- posts -------------------------------------------------------------

    async def list_posts(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        platform: str | None = None,
        status: str | None = None,
    ) -> PaginatedResponse[PostRead]:
        query = sa.select(SocialPost).where(SocialPost.tenant_id == tenant_id)
        if platform:
            query = query.where(SocialPost.platform == platform)
        if status:
            query = query.where(SocialPost.status == status)
        return await page_of(
            session,
            query.order_by(
                SocialPost.scheduled_at.desc().nullslast(), SocialPost.created_at.desc()
            ),
            PostRead.model_validate,
            limit,
            offset,
        )

    @staticmethod
    def _guard_schedule(platform: str, status: str, scheduled_at: datetime | None) -> None:
        if status != "scheduled":
            return
        if not supports_scheduled_publish(platform):
            raise manual_publish_only(platform)
        if scheduled_at is None:
            raise schedule_time_required()

    async def _assert_account(
        self, session: AsyncSession, tenant_id: UUID, account_id: UUID
    ) -> SocialAccount:
        account = await session.scalar(
            sa.select(SocialAccount).where(
                SocialAccount.tenant_id == tenant_id, SocialAccount.id == account_id
            )
        )
        if account is None:
            raise record_not_found("Social account")
        return account

    async def create_post(
        self, session: AsyncSession, tenant_id: UUID, actor_id: UUID, payload: PostCreate
    ) -> PostRead:
        self._guard_schedule(payload.platform, payload.status, payload.scheduled_at)
        if payload.social_account_id is not None:
            await self._assert_account(session, tenant_id, payload.social_account_id)
        post = SocialPost(
            tenant_id=tenant_id,
            social_account_id=payload.social_account_id,
            platform=payload.platform,
            title=payload.title,
            caption=payload.caption.strip(),
            hashtags=payload.hashtags,
            media_urls=payload.media_urls,
            sku_ref=payload.sku_ref,
            status=payload.status,
            scheduled_at=payload.scheduled_at,
            created_by=actor_id,
        )
        session.add(post)
        await session.flush()
        return PostRead.model_validate(post)

    async def _post_for_update(
        self, session: AsyncSession, tenant_id: UUID, post_id: UUID
    ) -> SocialPost:
        post = await session.scalar(
            sa.select(SocialPost)
            .where(SocialPost.tenant_id == tenant_id, SocialPost.id == post_id)
            .with_for_update()
        )
        if post is None:
            raise record_not_found("Social post")
        return post

    async def update_post(
        self, session: AsyncSession, tenant_id: UUID, post_id: UUID, payload: PostUpdate
    ) -> PostRead:
        post = await self._post_for_update(session, tenant_id, post_id)
        if payload.status is not None:
            if payload.status not in ("draft", "scheduled"):
                raise invalid_post_status(payload.status)
            if post.status == "published":
                raise invalid_post_status(payload.status)
            self._guard_schedule(
                post.platform, payload.status, payload.scheduled_at or post.scheduled_at
            )
            post.status = payload.status
        if payload.scheduled_at is not None:
            post.scheduled_at = payload.scheduled_at
        if payload.title is not None:
            post.title = payload.title
        if payload.caption is not None:
            post.caption = payload.caption.strip()
        if payload.hashtags is not None:
            post.hashtags = payload.hashtags
        if payload.media_urls is not None:
            post.media_urls = payload.media_urls
        if payload.sku_ref is not None:
            post.sku_ref = payload.sku_ref
        # Re-validate the stored state so a PATCH can never create a scheduled row
        # without a slot (the database CHECK is the second line of defence).
        self._guard_schedule(post.platform, post.status, post.scheduled_at)
        post.updated_at = datetime.now(UTC)
        await session.flush()
        return PostRead.model_validate(post)

    async def publish_post(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        post_id: UUID,
        payload: PostPublish,
    ) -> PostRead:
        post = await self._post_for_update(session, tenant_id, post_id)
        if post.status == "published":
            # Idempotent: a retried publish must not double-stamp published_at.
            return PostRead.model_validate(post)

        if supports_scheduled_publish(post.platform):
            account = (
                await self._assert_account(session, tenant_id, post.social_account_id)
                if post.social_account_id is not None
                else await session.scalar(
                    sa.select(SocialAccount).where(
                        SocialAccount.tenant_id == tenant_id,
                        SocialAccount.platform == post.platform,
                        SocialAccount.status == "connected",
                    )
                )
            )
            if account is None or account.status != "connected":
                raise account_not_connected(post.platform)
            post.social_account_id = account.id
            channel = "api"
        else:
            if not payload.manual_confirmation:
                raise manual_publish_only(post.platform)
            channel = "manual_console"

        post.status = "published"
        post.published_at = datetime.now(UTC)
        post.external_post_id = payload.external_post_id
        post.metrics = {**(post.metrics or {}), "publish_channel": channel}
        post.updated_at = datetime.now(UTC)
        await session.flush()
        return PostRead.model_validate(post)

    # -- mentions ----------------------------------------------------------

    async def list_mentions(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        platform: str | None = None,
        sentiment: str | None = None,
    ) -> PaginatedResponse[MentionRead]:
        query = sa.select(SocialMention).where(SocialMention.tenant_id == tenant_id)
        if platform:
            query = query.where(SocialMention.platform == platform)
        if sentiment:
            query = query.where(SocialMention.sentiment == sentiment)
        return await page_of(
            session,
            query.order_by(SocialMention.occurred_at.desc(), SocialMention.id),
            MentionRead.model_validate,
            limit,
            offset,
        )

    async def create_mention(
        self, session: AsyncSession, tenant_id: UUID, payload: MentionCreate
    ) -> MentionRead:
        if payload.social_account_id is not None:
            await self._assert_account(session, tenant_id, payload.social_account_id)
        mention = SocialMention(
            tenant_id=tenant_id,
            social_account_id=payload.social_account_id,
            platform=payload.platform,
            author_handle=payload.author_handle,
            content_text=payload.content_text,
            sentiment=payload.sentiment,
            occurred_at=payload.occurred_at or datetime.now(UTC),
        )
        session.add(mention)
        await session.flush()
        return MentionRead.model_validate(mention)

    async def convert_mention_to_lead(
        self, session: AsyncSession, tenant_id: UUID, mention_id: UUID
    ) -> ConvertLeadResult:
        """Atomically materialize `customers` + `leads` for one mention, exactly once."""
        mention = await session.scalar(
            sa.select(SocialMention)
            .where(SocialMention.tenant_id == tenant_id, SocialMention.id == mention_id)
            .with_for_update()
        )
        if mention is None:
            raise record_not_found("Social mention")
        if mention.converted_lead_id is not None:
            lead = await session.scalar(
                sa.select(Lead).where(
                    Lead.tenant_id == tenant_id, Lead.id == mention.converted_lead_id
                )
            )
            if lead is not None:
                return ConvertLeadResult(
                    mention_id=mention.id,
                    lead_id=lead.id,
                    customer_id=lead.customer_id,
                    already_converted=True,
                )

        customer = await session.scalar(
            sa.select(Customer)
            .where(
                Customer.tenant_id == tenant_id,
                Customer.display_name == mention.author_handle[:255],
            )
            .limit(1)
        )
        if customer is None:
            customer = Customer(
                tenant_id=tenant_id,
                customer_type="b2b_contact",
                display_name=mention.author_handle[:255],
                preferred_language="en" if mention.platform != "xiaohongshu" else "zh",
                lifecycle_stage="lead",
            )
            session.add(customer)
            await session.flush()

        lead = Lead(
            tenant_id=tenant_id,
            customer_id=customer.id,
            source_channel="social_mention",
            utm_campaign=None,
            lead_type="b2b_inquiry",
            qualification_score=_LEAD_SCORE.get(mention.sentiment, 40),
            extracted_requirements={
                "mention_id": str(mention.id),
                "platform": mention.platform,
                "author_handle": mention.author_handle,
                "sentiment": mention.sentiment,
                "content_text": mention.content_text[:2000],
                "occurred_at": mention.occurred_at.isoformat(),
            },
            status=LeadStatus.NEW,
        )
        session.add(lead)
        await session.flush()
        mention.converted_lead_id = lead.id
        await session.flush()
        return ConvertLeadResult(
            mention_id=mention.id,
            lead_id=lead.id,
            customer_id=customer.id,
            already_converted=False,
        )
