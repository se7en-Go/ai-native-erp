"""Admit the 17TRACK logistics provider to the raw webhook ingress only."""

from __future__ import annotations

from alembic import op

revision = "0003b_allow_17track_webhook"
down_revision = "0003"
branch_labels = None
depends_on = None

_ORIGINAL_CHANNELS = (
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
_CONSTRAINT = "ck_raw_webhook_events_channel_type"


def _set_allowlist(channels: tuple[str, ...]) -> None:
    literals = ", ".join(f"'{channel}'" for channel in channels)
    op.drop_constraint(_CONSTRAINT, "raw_webhook_events", type_="check")
    op.create_check_constraint(_CONSTRAINT, "raw_webhook_events", f"channel_type IN ({literals})")
    # Keep the 0002 function's validation, SECURITY DEFINER settings, and grants.
    op.execute(
        f"""
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
    )


def upgrade() -> None:
    _set_allowlist((*_ORIGINAL_CHANNELS, "17track"))


def downgrade() -> None:
    # If 17TRACK receipts still exist, constraint validation prevents a lossy downgrade.
    _set_allowlist(_ORIGINAL_CHANNELS)
