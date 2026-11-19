"""Add Phase 3D social marketing matrix, content calendar posts and mentions."""

from alembic import op

revision = "0008_phase3d_social_matrix"
down_revision = "0007_phase3c_growth_leads"
branch_labels = None
depends_on = None

_TABLES = (
    "social_accounts",
    "social_posts",
    "social_mentions",
)
_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_PLATFORMS = (
    "'instagram','tiktok','facebook','youtube','linkedin','pinterest','x',"
    "'xiaohongshu','douyin','kuaishou','bilibili','wechat_video','wechat_oa','weibo'"
)


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE social_accounts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            platform TEXT NOT NULL
                CONSTRAINT ck_social_accounts_platform CHECK (platform IN ({_PLATFORMS})),
            region TEXT NOT NULL
                CONSTRAINT ck_social_accounts_region CHECK (region IN ('overseas','domestic')),
            handle TEXT NOT NULL CHECK (length(btrim(handle)) > 0),
            display_name TEXT NOT NULL CHECK (length(btrim(display_name)) > 0),
            external_account_id TEXT,
            status TEXT NOT NULL DEFAULT 'connected'
                CONSTRAINT ck_social_accounts_status CHECK (
                    status IN ('connected','disconnected','token_expired')
                ),
            followers_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_social_accounts_followers_count CHECK (followers_count >= 0),
            config JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            token_ciphertext BYTEA,
            token_nonce BYTEA,
            token_auth_tag BYTEA,
            token_key_version INT,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, platform, handle),
            CHECK (jsonb_typeof(config) = 'object'),
            -- Envelope encryption is all-or-nothing: never a half-written secret.
            CONSTRAINT ck_social_accounts_token_envelope CHECK (
                (
                    token_ciphertext IS NULL AND token_nonce IS NULL
                    AND token_auth_tag IS NULL AND token_key_version IS NULL
                )
                OR (
                    token_ciphertext IS NOT NULL AND token_nonce IS NOT NULL
                    AND token_auth_tag IS NOT NULL AND token_key_version IS NOT NULL
                )
            )
        )
    """)
    op.execute(f"""
        CREATE TABLE social_posts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            social_account_id UUID REFERENCES social_accounts(id) ON DELETE SET NULL,
            platform TEXT NOT NULL
                CONSTRAINT ck_social_posts_platform CHECK (platform IN ({_PLATFORMS})),
            title TEXT,
            caption TEXT NOT NULL CHECK (length(btrim(caption)) > 0),
            hashtags JSONB NOT NULL DEFAULT '[]'::jsonb,
            media_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
            sku_ref TEXT,
            status TEXT NOT NULL DEFAULT 'draft'
                CONSTRAINT ck_social_posts_status CHECK (
                    status IN ('draft','scheduled','published','failed')
                ),
            scheduled_at TIMESTAMPTZ,
            published_at TIMESTAMPTZ,
            external_post_id TEXT,
            metrics JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            CHECK (jsonb_typeof(hashtags) = 'array'),
            CHECK (jsonb_typeof(media_urls) = 'array'),
            CHECK (jsonb_typeof(metrics) = 'object'),
            -- A scheduled row without a slot would silently never publish.
            CONSTRAINT ck_social_posts_scheduled_at CHECK (
                status <> 'scheduled' OR scheduled_at IS NOT NULL
            ),
            CONSTRAINT ck_social_posts_published_at CHECK (
                status <> 'published' OR published_at IS NOT NULL
            )
        )
    """)
    op.execute(f"""
        CREATE TABLE social_mentions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            social_account_id UUID REFERENCES social_accounts(id) ON DELETE SET NULL,
            platform TEXT NOT NULL
                CONSTRAINT ck_social_mentions_platform CHECK (platform IN ({_PLATFORMS})),
            author_handle TEXT NOT NULL CHECK (length(btrim(author_handle)) > 0),
            content_text TEXT NOT NULL CHECK (length(btrim(content_text)) > 0),
            sentiment TEXT NOT NULL DEFAULT 'neutral'
                CONSTRAINT ck_social_mentions_sentiment CHECK (
                    sentiment IN ('positive','neutral','negative','lead_intent')
                ),
            converted_lead_id UUID,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        )
    """)
    for table, index_columns in (
        ("social_accounts", "tenant_id, platform, status"),
        ("social_posts", "tenant_id, status, scheduled_at"),
        ("social_posts", "tenant_id, platform, created_at"),
        ("social_mentions", "tenant_id, sentiment, occurred_at"),
        ("social_mentions", "tenant_id, platform, occurred_at"),
    ):
        columns = [part.strip() for part in index_columns.split(",")]
        op.create_index(f"ix_{table}_{'_'.join(columns[1:])}", table, columns)
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_allow_runtime ON {table} AS PERMISSIVE "
            "FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
        )
        op.execute(
            f"CREATE POLICY tenant_isolation_policy ON {table} AS RESTRICTIVE "
            f"FOR ALL TO app_runtime USING ({_SCOPE}) WITH CHECK ({_SCOPE})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_runtime")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
