"""Tenant-scoped registration of storefront webhook routing.

`channel_routing_directory` is FORCE-RLS with no runtime write path. Connectors therefore
register their store domain through one validated SECURITY DEFINER function; the narrow
`app_migrator` INSERT/UPDATE policies it needs are bound to transaction-scoped GUCs that
only this function sets.
"""

from __future__ import annotations

from alembic import op

revision = "0003c_channel_routing"
down_revision = "0003b_allow_17track_webhook"
branch_labels = None
depends_on = None

_CHANNELS = (
    "whatsapp_cloud", "whatsapp_coexistence", "email", "web_livechat", "shopify",
    "woocommerce", "shopline", "shoplazza", "tiktok_shop", "made_in_china_email",
    "alibaba_email", "messenger", "instagram_dm", "telegram", "amazon_sp", "ebay",
    "mercado_libre", "temu", "walmart", "shopee", "lazada",
)
_CHANNEL_SQL_LIST = ", ".join(f"'{channel}'" for channel in _CHANNELS)
_FUNCTION = "register_channel_routing(TEXT, TEXT, UUID, UUID, BOOLEAN)"
# Resolution and the function's ownership probe share these lookup GUCs.
_WRITE_PREDICATE = (
    "channel_type = current_setting('app.routing_write_channel', true) "
    "AND external_account_id = current_setting('app.routing_write_external', true) "
    "AND tenant_id = NULLIF(current_setting('app.routing_write_tenant', true), '')::uuid "
    "AND current_setting('app.routing_write_channel', true) <> '' "
    "AND current_setting('app.routing_write_tenant', true) <> ''"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE POLICY migrator_register_permissive ON channel_routing_directory
        AS PERMISSIVE FOR INSERT TO app_migrator
        WITH CHECK ({_WRITE_PREDICATE})
        """
    )
    op.execute(
        f"""
        CREATE POLICY migrator_update_permissive ON channel_routing_directory
        AS PERMISSIVE FOR UPDATE TO app_migrator
        USING ({_WRITE_PREDICATE})
        WITH CHECK ({_WRITE_PREDICATE})
        """
    )
    op.execute(
        f"""
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
            IF p_channel_type NOT IN ({_CHANNEL_SQL_LIST}) THEN
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
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO app_runtime")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM app_runtime")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}")
    op.execute("DROP POLICY IF EXISTS migrator_update_permissive ON channel_routing_directory")
    op.execute("DROP POLICY IF EXISTS migrator_register_permissive ON channel_routing_directory")
