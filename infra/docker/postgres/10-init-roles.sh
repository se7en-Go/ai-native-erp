#!/usr/bin/env bash
#
# Production Postgres bootstrap for ai-native-erp (runs once, on an empty volume).
#
# Creates the same strict role split as the development init-roles.sql, but
# reads the passwords from the environment so no credential is ever committed:
#
#   app_migrator -> DDL only (Alembic `upgrade head`), never used at runtime
#   app_runtime  -> LOGIN, NOSUPERUSER, NOBYPASSRLS, row security always applies
#
# The extension set (pgcrypto / vector / pg_trgm) is installed here as the
# superuser because app_migrator is not allowed to create extensions.

set -euo pipefail

: "${APP_MIGRATOR_USER:=app_migrator}"
: "${APP_RUNTIME_USER:=app_runtime}"
: "${APP_MIGRATOR_PASSWORD:?APP_MIGRATOR_PASSWORD must be set (see .env.prod.example)}"
: "${APP_RUNTIME_PASSWORD:?APP_RUNTIME_PASSWORD must be set (see .env.prod.example)}"

psql --no-password --set=ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER:-postgres}" \
    --dbname "${POSTGRES_DB:-erp_saas}" \
    --set=migrator_user="${APP_MIGRATOR_USER}" \
    --set=migrator_password="${APP_MIGRATOR_PASSWORD}" \
    --set=runtime_user="${APP_RUNTIME_USER}" \
    --set=runtime_password="${APP_RUNTIME_PASSWORD}" <<'SQL'
-- Idempotent role creation without printing the password anywhere.
SELECT format('CREATE ROLE %I NOINHERIT LOGIN PASSWORD %L',
              :'migrator_user', :'migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migrator_user')
\gexec

SELECT format('CREATE ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT LOGIN PASSWORD %L',
              :'runtime_user', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_user')
\gexec

-- A runtime role that can bypass RLS would silently break tenant isolation.
SELECT format('ALTER ROLE %I NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE', :'runtime_user')
\gexec

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', current_database(), :'migrator_user')
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'migrator_user')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'runtime_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'runtime_user')
\gexec

-- Audit/RBAC helper functions are granted per-migration; keep PUBLIC locked out.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'migrator_user')
\gexec
SQL

echo "postgres: app_migrator/app_runtime roles and extensions are ready"
