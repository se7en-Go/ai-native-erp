#!/usr/bin/env bash
# Track 8 / Ops — verified restore of a custom-format dump into a target database.
#
#   ./restore.sh /var/backups/erp/erp_saas_20261126T010203Z.dump
#   ./restore.sh <dump> --database erp_staging
#   ./restore.sh <dump> --database erp_saas --yes-overwrite-prod   # DANGEROUS
#
# Safety rules (fail closed):
#   * the dump file must exist, be non-empty and match its `.sha256` sidecar
#     (or `EXPECTED_SHA256`) before a single object is touched;
#   * the production database name (`PROD_DB`, default `erp_saas`) can only be
#     overwritten when `--yes-overwrite-prod` is passed explicitly;
#   * the target database is created when missing, then restored inside a single
#     transaction with `--clean --if-exists --no-owner`;
#   * afterwards the `alembic_version` table is queried (the exact row that
#     `alembic current` reports) and `ALEMBIC_CMD` is executed when provided.
#
# Overridable environment:
#   PG_CONTAINER   default erp_prod_postgres ("" => local pg_restore/psql)
#   PG_USER        default postgres
#   PGHOST/PGPORT/PGPASSWORD   used when PG_CONTAINER is empty
#   PROD_DB        default erp_saas
#   RESTORE_DB     default erp_restore_verify (non-prod)
#   ALEMBIC_CMD    optional, e.g. "uv run --directory apps/backend alembic current"
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER-erp_prod_postgres}"
PG_USER="${PG_USER:-postgres}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PROD_DB="${PROD_DB:-erp_saas}"
TARGET_DB="${RESTORE_DB:-erp_restore_verify}"
ALEMBIC_CMD="${ALEMBIC_CMD:-}"

usage() {
  cat <<'USAGE'
Usage: restore.sh <backup_file> [--database NAME] [--yes-overwrite-prod]

  <backup_file>            pg_dump -Fc file produced by backup.sh
  --database NAME          target database (default: $RESTORE_DB or erp_restore_verify)
  --yes-overwrite-prod     required to restore into $PROD_DB (default erp_saas)
USAGE
}

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

backup_file=""
overwrite_prod=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --database)
      [ "$#" -ge 2 ] || die "--database requires a value" 2
      TARGET_DB="$2"
      shift 2
      ;;
    --database=*)
      TARGET_DB="${1#--database=}"
      shift
      ;;
    --yes-overwrite-prod)
      overwrite_prod=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      die "unknown option '$1' (see --help)" 2
      ;;
    *)
      [ -z "$backup_file" ] || die "only one backup file may be given" 2
      backup_file="$1"
      shift
      ;;
  esac
done

[ -n "$backup_file" ] || {
  usage
  die "missing <backup_file>" 2
}
[ -f "$backup_file" ] || die "backup file '$backup_file' does not exist"
[ -s "$backup_file" ] || die "backup file '$backup_file' is empty"

if [ "$TARGET_DB" = "$PROD_DB" ] && [ "$overwrite_prod" -ne 1 ]; then
  die "refusing to overwrite production database '$PROD_DB' without --yes-overwrite-prod" 3
fi
[[ "$TARGET_DB" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "invalid database name '$TARGET_DB'" 2

sha256_of() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
    return
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
    return
  fi
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r "$file" | awk '{print $1}'
    return
  fi
  die "No SHA-256 tool available (sha256sum / shasum / openssl)."
}

expected_checksum="$(sha256_of "$backup_file")"
if [ -n "${EXPECTED_SHA256:-}" ]; then
  recorded="${EXPECTED_SHA256%% *}"
elif [ -f "$backup_file.sha256" ]; then
  recorded="$(awk '{print $1}' "$backup_file.sha256" | head -n 1)"
else
  die "no checksum sidecar '$backup_file.sha256' and no EXPECTED_SHA256 given" 4
fi
[ -n "$recorded" ] || die "checksum sidecar '$backup_file.sha256' is empty" 4
if [ "$recorded" != "$expected_checksum" ]; then
  die "checksum mismatch: expected $recorded, file is $expected_checksum" 4
fi
say "checksum OK ($expected_checksum)"

psql_run() {
  # psql_run <database> <sql> [-t] : one-shot SQL against the target server.
  local database="$1" sql="$2"
  if [ -n "$PG_CONTAINER" ]; then
    docker exec -i -e "PGPASSWORD=${PGPASSWORD:-}" "$PG_CONTAINER" \
      psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$database" -c "$sql"
    return
  fi
  PGPASSWORD="${PGPASSWORD:-}" psql -v ON_ERROR_STOP=1 \
    -h "$PGHOST" -p "$PGPORT" -U "$PG_USER" -d "$database" -c "$sql"
}

psql_scalar() {
  local database="$1" sql="$2"
  if [ -n "$PG_CONTAINER" ]; then
    docker exec -i -e "PGPASSWORD=${PGPASSWORD:-}" "$PG_CONTAINER" \
      psql -tAq -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$database" -c "$sql"
    return
  fi
  PGPASSWORD="${PGPASSWORD:-}" psql -tAq -v ON_ERROR_STOP=1 \
    -h "$PGHOST" -p "$PGPORT" -U "$PG_USER" -d "$database" -c "$sql"
}

say "== ERP PostgreSQL restore =="
say "source: $backup_file"
say "target: $TARGET_DB"

say "1/5 ensure target database exists ..."
exists="$(psql_scalar postgres "SELECT 1 FROM pg_database WHERE datname = '$TARGET_DB'")"
if [ "$exists" = "1" ]; then
  say "      database '$TARGET_DB' already exists"
else
  psql_run postgres "CREATE DATABASE \"$TARGET_DB\"" >/dev/null
  say "      created database '$TARGET_DB'"
fi

restore_command=(pg_restore --clean --if-exists --no-owner --single-transaction
  --dbname "$TARGET_DB" --username "$PG_USER")

say "2/5 restore (pg_restore --clean --if-exists --single-transaction) ..."
if [ -n "$PG_CONTAINER" ]; then
  docker exec -i -e "PGPASSWORD=${PGPASSWORD:-}" "$PG_CONTAINER" \
    "${restore_command[@]}" <"$backup_file"
else
  PGPASSWORD="${PGPASSWORD:-}" PGHOST="$PGHOST" PGPORT="$PGPORT" \
    "${restore_command[@]}" <"$backup_file"
fi

say "3/5 verify table count ..."
tables="$(psql_scalar "$TARGET_DB" \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
[ "${tables:-0}" -gt 0 ] || die "restore produced zero public tables" 5
say "      public tables: $tables"

say "4/5 verify fail-closed RLS on restored tenant tables ..."
for table in tenants orders; do
  rls="$(psql_scalar "$TARGET_DB" \
    "SELECT relrowsecurity FROM pg_class WHERE relname = '$table'")"
  if [ "$rls" = "t" ]; then
    say "      $table: rowsecurity = true"
  else
    die "RLS is not enabled on restored table '$table' (relrowsecurity=$rls)" 6
  fi
done

say "5/5 verify alembic revision ..."
version="$(psql_scalar "$TARGET_DB" "SELECT version_num FROM alembic_version")"
[ -n "$version" ] || die "restored database has an empty alembic_version table" 7
say "      alembic_version: $version"
if [ -n "$ALEMBIC_CMD" ]; then
  say "      running ALEMBIC_CMD: $ALEMBIC_CMD"
  # shellcheck disable=SC2086 # intentional word splitting of the operator command
  $ALEMBIC_CMD
fi

say "RESULT: restore of $backup_file into '$TARGET_DB' verified (revision $version)."
