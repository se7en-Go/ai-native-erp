"""Admit SHEIN, AliExpress, and Ozon to every channel allowlist and definer function.

Three new global marketplaces join the existing 21 channels on the four tenant-facing
tables (`channel_routing_directory`, `channel_accounts`, `conversations`, `messages`) and
on the webhook ingress table `raw_webhook_events`, which additionally keeps the `17track`
logistics pseudo-channel admitted by `0003b_allow_17track_webhook`.

The three SECURITY DEFINER helpers are recreated because their allowlists are compiled
into the function bodies; guards, validation, `SET search_path = public, pg_temp`, and
grants are carried over unchanged.

Revision ID: 0018_marketplace_channels
Revises: 0017_collector_api_tokens
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_marketplace_channels"
down_revision: str | None = "0017_collector_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The 21 channels locked in `0002_channels_identity_inbox` (and `0003c`).
_LEGACY_CHANNELS: tuple[str, ...] = (
    "whatsapp_cloud",
    "whatsapp_coexistence",
    "email",
    "web_livechat",
    "shopify",
    "woocommerce",
    "shopline",
    "shoplazza",
    "tiktok_shop",
    "made_in_china_email",
    "alibaba_email",
    "messenger",
    "instagram_dm",
    "telegram",
    "amazon_sp",
    "ebay",
    "mercado_libre",
    "temu",
    "walmart",
    "shopee",
    "lazada",
)
#: Newly admitted global marketplaces.
_MARKETPLACE_CHANNELS: tuple[str, ...] = ("shein", "aliexpress", "ozon")
_CHANNELS: tuple[str, ...] = (*_LEGACY_CHANNELS, *_MARKETPLACE_CHANNELS)
#: `raw_webhook_events` / `accept_raw_webhook_event` also carry 17TRACK receipts.
_LEGACY_WEBHOOK_CHANNELS: tuple[str, ...] = (*_LEGACY_CHANNELS, "17track")
_WEBHOOK_CHANNELS: tuple[str, ...] = (*_CHANNELS, "17track")

#: (table, constraint) pairs whose allowlist does NOT include the logistics pseudo-channel.
_CHANNEL_TYPE_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("channel_routing_directory", "ck_channel_routing_directory_channel_type"),
    ("channel_accounts", "ck_channel_accounts_channel_type"),
    ("conversations", "ck_conversations_channel_type"),
    ("messages", "ck_messages_channel_type"),
)
_WEBHOOK_TABLE = "raw_webhook_events"
_WEBHOOK_CONSTRAINT = "ck_raw_webhook_events_channel_type"

_RESOLVE_FUNCTION = "resolve_tenant_by_channel_external_id(TEXT, TEXT)"
_ACCEPT_FUNCTION = "accept_raw_webhook_event(UUID, UUID, TEXT, TEXT, TEXT, JSONB, JSONB)"
_REGISTER_FUNCTION = "register_channel_routing(TEXT, TEXT, UUID, UUID, BOOLEAN)"


def _sql_list(channels: tuple[str, ...]) -> str:
    return ", ".join(f"'{channel}'" for channel in channels)


def _set_allowlist(table: str, constraint: str, channels: tuple[str, ...]) -> None:
    literals = _sql_list(channels)
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, f"channel_type IN ({literals})")


def _resolve_tenant_sql(channels: tuple[str, ...]) -> str:
    """`resolve_tenant_by_channel_external_id` with the given allowlist."""
    literals = _sql_list(channels)
    return f"""
        CREATE OR REPLACE FUNCTION resolve_tenant_by_channel_external_id(
            p_channel_type TEXT,
            p_external_id TEXT
        )
        RETURNS TABLE (tenant_id UUID, channel_account_id UUID)
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
            IF p_channel_type IS NULL OR p_external_id IS NULL THEN
                RETURN;
            END IF;
            IF p_channel_type NOT IN ({literals}) THEN
                RETURN;
            END IF;
            IF length(p_external_id) = 0 OR length(p_external_id) > 256 THEN
                RETURN;
            END IF;
            IF p_external_id ~ '[^A-Za-z0-9_.:@+\\-]' THEN
                RETURN;
            END IF;
            PERFORM set_config('app.routing_lookup_channel', p_channel_type, true);
            PERFORM set_config('app.routing_lookup_external', p_external_id, true);
            RETURN QUERY
                SELECT crd.tenant_id, crd.channel_account_id
                FROM public.channel_routing_directory crd
                WHERE crd.channel_type = p_channel_type
                  AND crd.external_account_id = p_external_id
                  AND crd.is_active = true
                LIMIT 1;
        END;
        $fn$;
        """


def _accept_raw_webhook_sql(channels: tuple[str, ...]) -> str:
    """`accept_raw_webhook_event` with the given (17TRACK-inclusive) allowlist."""
    literals = _sql_list(channels)
    return f"""
        CREATE OR REPLACE FUNCTION accept_raw_webhook_event(
            p_id UUID,
            p_tenant_id UUID,
            p_channel_type TEXT,
            p_external_event_id TEXT,
            p_external_account_id TEXT,
            p_headers JSONB,
            p_payload JSONB
        ) RETURNS UUID
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            inserted_id UUID;
            existing_id UUID;
            existing_status TEXT;
        BEGIN
            IF p_channel_type IS NULL OR p_external_event_id IS NULL THEN
                RAISE EXCEPTION 'webhook identity required' USING ERRCODE = '22023';
            END IF;
            IF p_channel_type NOT IN ({literals}) THEN
                RAISE EXCEPTION 'unsupported channel' USING ERRCODE = '22023';
            END IF;
            IF length(p_external_event_id) = 0 OR length(p_external_event_id) > 512 THEN
                RAISE EXCEPTION 'event id length' USING ERRCODE = '22023';
            END IF;
            IF p_external_account_id IS NOT NULL
               AND length(p_external_account_id) > 256 THEN
                RAISE EXCEPTION 'account id length' USING ERRCODE = '22023';
            END IF;
            IF octet_length(coalesce(p_payload, '{{}}'::jsonb)::text) > 1048576
               OR octet_length(coalesce(p_headers, '{{}}'::jsonb)::text) > 65536 THEN
                RAISE EXCEPTION 'webhook payload too large' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.raw_webhook_events (
                id, tenant_id, channel_type, external_event_id, external_account_id,
                headers, raw_payload, signature_verified, processing_status
            ) VALUES (
                p_id, p_tenant_id, p_channel_type, p_external_event_id,
                p_external_account_id, coalesce(p_headers, '{{}}'::jsonb),
                coalesce(p_payload, '{{}}'::jsonb), true, 'received'
            )
            ON CONFLICT (channel_type, external_event_id) DO NOTHING
            RETURNING id INTO inserted_id;
            IF inserted_id IS NOT NULL THEN
                RETURN inserted_id;
            END IF;
            SELECT id, processing_status INTO existing_id, existing_status
            FROM public.raw_webhook_events
            WHERE channel_type = p_channel_type
              AND external_event_id = p_external_event_id;
            IF existing_status = 'received' THEN
                RETURN existing_id;
            END IF;
            RETURN NULL;
        END;
        $fn$;
        """


def _register_channel_routing_sql(channels: tuple[str, ...]) -> str:
    """`register_channel_routing` with the given allowlist (17TRACK is not routable)."""
    literals = _sql_list(channels)
    return f"""
        CREATE OR REPLACE FUNCTION register_channel_routing(
            p_channel_type TEXT,
            p_external_account_id TEXT,
            p_tenant_id UUID,
            p_channel_account_id UUID,
            p_is_active BOOLEAN DEFAULT true
        ) RETURNS TEXT
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            v_caller_tenant UUID;
            v_owner_tenant UUID;
        BEGIN
            IF p_channel_type IS NULL OR p_external_account_id IS NULL
               OR p_tenant_id IS NULL OR p_channel_account_id IS NULL THEN
                RAISE EXCEPTION 'channel routing identity required' USING ERRCODE = '22023';
            END IF;
            IF p_channel_type NOT IN ({literals}) THEN
                RAISE EXCEPTION 'unsupported channel' USING ERRCODE = '22023';
            END IF;
            IF length(p_external_account_id) = 0 OR length(p_external_account_id) > 256
               OR p_external_account_id ~ '[^A-Za-z0-9_.:@+\\-]' THEN
                RAISE EXCEPTION 'invalid external account id' USING ERRCODE = '22023';
            END IF;
            v_caller_tenant := NULLIF(current_setting('app.current_tenant_id', true), '')::uuid;
            IF v_caller_tenant IS NULL OR v_caller_tenant <> p_tenant_id THEN
                RAISE EXCEPTION 'channel routing tenant context mismatch' USING ERRCODE = '42501';
            END IF;

            PERFORM set_config('app.routing_lookup_channel', p_channel_type, true);
            PERFORM set_config('app.routing_lookup_external', p_external_account_id, true);
            PERFORM set_config('app.routing_write_channel', p_channel_type, true);
            PERFORM set_config('app.routing_write_external', p_external_account_id, true);
            PERFORM set_config('app.routing_write_tenant', p_tenant_id::text, true);

            SELECT crd.tenant_id INTO v_owner_tenant
            FROM public.channel_routing_directory crd
            WHERE crd.channel_type = p_channel_type
              AND crd.external_account_id = p_external_account_id;
            IF v_owner_tenant IS NOT NULL AND v_owner_tenant <> p_tenant_id THEN
                RAISE EXCEPTION 'channel routing owned by another tenant' USING ERRCODE = '23505';
            END IF;

            INSERT INTO public.channel_routing_directory (
                channel_type, external_account_id, tenant_id, channel_account_id, is_active
            ) VALUES (
                p_channel_type, p_external_account_id, p_tenant_id,
                p_channel_account_id, coalesce(p_is_active, true)
            )
            ON CONFLICT (channel_type, external_account_id) DO UPDATE
            SET channel_account_id = EXCLUDED.channel_account_id,
                is_active = EXCLUDED.is_active
            WHERE public.channel_routing_directory.tenant_id = p_tenant_id;
            RETURN 'registered';
        END;
        $fn$;
        """


def _apply_allowlists(
    channels: tuple[str, ...],
    webhook_channels: tuple[str, ...],
) -> None:
    """Rewrite every CHECK constraint, then the three SECURITY DEFINER bodies."""
    for table, constraint in _CHANNEL_TYPE_CONSTRAINTS:
        _set_allowlist(table, constraint, channels)
    _set_allowlist(_WEBHOOK_TABLE, _WEBHOOK_CONSTRAINT, webhook_channels)

    for function, sql in (
        (_RESOLVE_FUNCTION, _resolve_tenant_sql(channels)),
        (_ACCEPT_FUNCTION, _accept_raw_webhook_sql(webhook_channels)),
        (_REGISTER_FUNCTION, _register_channel_routing_sql(channels)),
    ):
        op.execute(sql)
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO app_runtime")


def upgrade() -> None:
    _apply_allowlists(_CHANNELS, _WEBHOOK_CHANNELS)


def downgrade() -> None:
    # A marketplace row still present makes the narrowed CHECK fail validation, which is
    # the intended lossy-downgrade guard. 17TRACK stays admitted on the webhook ingress.
    _apply_allowlists(_LEGACY_CHANNELS, _LEGACY_WEBHOOK_CHANNELS)
