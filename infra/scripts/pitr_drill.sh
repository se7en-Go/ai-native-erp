#!/usr/bin/env bash
# Track 8 / Ops — non-destructive backup + restore (PITR readiness) drill.
#
# Proves, on every run, that the nightly backup is actually restorable and that
# the restored copy still enforces fail-closed RLS:
#
#   1. `backup.sh` runs into a throw-away directory (the real backup archive is
#      untouched);
#   2. an ephemeral database (`erp_pitr_drill`) is created;
#   3. the dump is checksum-verified and restored into that database;
#   4. public table count, `rowsecurity = true` on `tenants` / `orders` and the
#      per-table row counts are compared against the live source database;
#   5. the drill database is dropped and the temporary dump removed — success or
#      failure, the trap always cleans up.
#
# Overridable environment:
#   DRILL_DB       default erp_pitr_drill
#   PG_CONTAINER   default erp_prod_postgres ("" => local tools)
#   PG_USER        default postgres
#   PG_DATABASE    default erp_saas (source database)
#   PGHOST/PGPORT/PGPASSWORD  used when PG_CONTAINER is empty
#   DRILL_TABLES   default "tenants orders order_items products"
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DRILL_DB="${DRILL_DB:-erp_pitr_drill}"
PG_CONTAINER="${PG_CONTAINER-erp_prod_postgres}"
PG_USER="${PG_USER:-postgres}"
PG_DATABASE="${PG_DATABASE:-erp_saas}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
DRILL_TABLES="${DRILL_TABLES:-tenants orders order_items products}"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

[ "$DRILL_DB" != "$PG_DATABASE" ] || die "DRILL_DB must differ from the source database" 2
[[ "$DRILL_DB" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "invalid DRILL_DB '$DRILL_DB'" 2

workdir="$(mktemp -d "${TMPDIR:-/tmp}/erp_pitr_drill.XXXXXX")"
dump_file=""

drop_drill_db() {
  if [ -n "$PG_CONTAINER" ]; then
    docker exec -i -e "PGPASSWORD=${PGPASSWORD:-}" "$PG_CONTAINER" \
      psql -tAq -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres \
      -c "DROP DATABASE IF EXISTS \"$DRILL_DB\" WITH (FORCE)" >/dev/null 2>&1 || true
  else
    PGPASSWORD="${PGPASSWORD:-}" psql -tAq -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" \
      -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$DRILL_DB\" WITH (FORCE)" \
      >/dev/null 2>&1 || true
  fi
}

cleanup() {
  status=$?
  drop_drill_db
  if [ -n "$dump_file" ] && [ -f "$dump_file" ]; then
    rm -f "$dump_file" "$dump_file.sha256"
  fi
  rm -rf "$workdir"
  if [ "$status" -eq 0 ]; then
    say "cleanup: drill database '$DRILL_DB' dropped, temporary dump removed."
  else
    say "cleanup: after failure, drill database '$DRILL_DB' dropped, temporary dump removed."
  fi
  exit "$status"
}
trap cleanup EXIT

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

say "== ERP PITR / restore drill =="
say "source database: $PG_DATABASE"
say "drill database:  $DRILL_DB"
say "work directory:  $workdir"

say "1/6 run backup.sh into the throw-away directory ..."
BACKUP_DIR="$workdir" BACKUP_RETENTION_DAYS=1 \
  PG_CONTAINER="$PG_CONTAINER" PG_USER="$PG_USER" PG_DATABASE="$PG_DATABASE" \
  PGHOST="$PGHOST" PGPORT="$PGPORT" \
  "$SCRIPT_DIR/backup.sh"
dump_file="$(find "$workdir" -maxdepth 1 -type f -name '*.dump' -print -quit)"
[ -n "$dump_file" ] && [ -s "$dump_file" ] || die "backup.sh did not produce a dump" 3
say "      dump: $dump_file"

say "2/6 record source row counts ..."
declare -a tables=()
declare -a source_counts=()
for table in $DRILL_TABLES; do
  count="$(psql_scalar "$PG_DATABASE" "SELECT count(*) FROM \"$table\"")"
  tables+=("$table")
  source_counts+=("$count")
  say "      source $table: $count row(s)"
done

say "3/6 restore the dump into the ephemeral '$DRILL_DB' database ..."
PG_CONTAINER="$PG_CONTAINER" PG_USER="$PG_USER" PGHOST="$PGHOST" PGPORT="$PGPORT" \
  "$SCRIPT_DIR/restore.sh" "$dump_file" --database "$DRILL_DB"

say "4/6 compare table counts ..."
source_tables="$(psql_scalar "$PG_DATABASE" \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
drill_tables="$(psql_scalar "$DRILL_DB" \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
say "      public tables: source=$source_tables drill=$drill_tables"
[ "$source_tables" = "$drill_tables" ] ||
  die "table count mismatch (source $source_tables != drill $drill_tables)" 4

say "5/6 compare row counts and RLS flags ..."
index=0
while [ "$index" -lt "${#tables[@]}" ]; do
  table="${tables[$index]}"
  expected="${source_counts[$index]}"
  actual="$(psql_scalar "$DRILL_DB" "SELECT count(*) FROM \"$table\"")"
  [ "$expected" = "$actual" ] ||
    die "row count mismatch on '$table' (source $expected != drill $actual)" 5
  rls="$(psql_scalar "$DRILL_DB" \
    "SELECT relrowsecurity FROM pg_class WHERE relname = '$table'")"
  if [ "$rls" = "t" ]; then
    say "      $table: $actual row(s), rowsecurity = true"
  else
    say "      $table: $actual row(s), rowsecurity = false (RLS not required here)"
  fi
  index=$((index + 1))
done

for required in tenants orders; do
  rls="$(psql_scalar "$DRILL_DB" \
    "SELECT relrowsecurity FROM pg_class WHERE relname = '$required'")"
  [ "$rls" = "t" ] || die "restored '$required' lost fail-closed RLS" 6
done

say "6/6 drill verified."
say "RESULT: PITR drill passed — backup is restorable, RLS survived, row counts match."

drop_drill_db
dump_file=""
trap - EXIT
rm -rf "$workdir"
say "cleanup: drill database '$DRILL_DB' dropped, temporary dump removed."
