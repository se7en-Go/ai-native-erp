-- Strictly separate migration owner (DDL) and runtime application role (no SUPERUSER, no BYPASSRLS)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator NOINHERIT LOGIN PASSWORD 'migrator_dev_pass';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        CREATE ROLE app_runtime NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN PASSWORD 'runtime_dev_pass';
    END IF;
END
$$;

-- Pre-install required PostgreSQL 17 extensions as superuser during DB init
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

GRANT ALL PRIVILEGES ON DATABASE erp_saas TO app_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;

GRANT CONNECT ON DATABASE erp_saas TO app_runtime;
GRANT USAGE ON SCHEMA public TO app_runtime;
