#!/usr/bin/env bash
# Track 8 / Ops — timestamped custom-format PostgreSQL backup with verification.
#
# Produces, atomically:
#   $BACKUP_DIR/erp_saas_<UTC timestamp>.dump          (pg_dump -Fc)
#   $BACKUP_DIR/erp_saas_<UTC timestamp>.dump.sha256   (checksum sidecar)
#
# Every dump is re-read with `pg_restore --list` before it is promoted, so a
# truncated/partial dump never looks like a valid backup, and backups older than
# BACKUP_RETENTION_DAYS (default 14) are pruned together with their checksum.
#
# Overridable environment:
#   BACKUP_DIR            default /var/backups/erp
#   BACKUP_RETENTION_DAYS default 14
#   PG_CONTAINER          default erp_prod_postgres ("" => local pg_dump)
#   PG_USER               default postgres
#   PG_DATABASE           default erp_saas
#   PGHOST/PGPORT/PGPASSWORD  used when PG_CONTAINER is empty
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/erp}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
PG_CONTAINER="${PG_CONTAINER-erp_prod_postgres}"
PG_USER="${PG_USER:-postgres}"
PG_DATABASE="${PG_DATABASE:-erp_saas}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"

if ! [[ "$BACKUP_RETENTION_DAYS" =~ ^[0-9]+$ ]] || [ "$BACKUP_RETENTION_DAYS" -lt 1 ]; then
  echo "BACKUP_RETENTION_DAYS must be a positive integer (got '$BACKUP_RETENTION_DAYS')." >&2
  exit 2
fi

say() { printf '%s\n' "$*"; }

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
  echo "No SHA-256 tool available (sha256sum / shasum / openssl)." >&2
  return 1
}

pg_dump_stream() {
  if [ -n "$PG_CONTAINER" ]; then
    docker exec -i -e "PGPASSWORD=${PGPASSWORD:-}" "$PG_CONTAINER" \
      pg_dump -Fc -U "$PG_USER" -d "$PG_DATABASE"
    return
  fi
  PGHOST="$PGHOST" PGPORT="$PGPORT" PGPASSWORD="${PGPASSWORD:-}" \
    pg_dump -Fc -U "$PG_USER" -d "$PG_DATABASE"
}

verify_dump() {
  local file="$1"
  if [ -n "$PG_CONTAINER" ]; then
    # `pg_restore --list` with no filename argument reads the archive from stdin.
    docker exec -i "$PG_CONTAINER" pg_restore --list <"$file" >/dev/null
    return
  fi
  pg_restore --list "$file" >/dev/null
}

mkdir -p "$BACKUP_DIR"
if [ ! -w "$BACKUP_DIR" ]; then
  echo "Backup directory '$BACKUP_DIR' is not writable." >&2
  exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
basename="${PG_DATABASE}_${timestamp}.dump"
target="$BACKUP_DIR/$basename"
partial="$target.partial"

say "== ERP PostgreSQL backup =="
say "database:  $PG_DATABASE"
say "target:    $target"

cleanup_partial() {
  if [ -f "$partial" ]; then
    rm -f "$partial"
  fi
}
trap cleanup_partial EXIT

say "1/4 dump (custom format -Fc) ..."
pg_dump_stream >"$partial"
if [ ! -s "$partial" ]; then
  echo "pg_dump produced an empty file; refusing to publish it." >&2
  exit 1
fi

say "2/4 verify dump integrity (pg_restore --list) ..."
if ! verify_dump "$partial"; then
  echo "Dump failed pg_restore --list verification; refusing to publish it." >&2
  exit 1
fi

mv "$partial" "$target"
checksum="$(sha256_of "$target")"
printf '%s  %s\n' "$checksum" "$basename" >"$target.sha256"
say "      sha256 $checksum"

say "3/4 prune backups older than ${BACKUP_RETENTION_DAYS} day(s) ..."
pruned=0
while IFS= read -r stale; do
  [ -n "$stale" ] || continue
  rm -f "$stale" "$stale.sha256"
  pruned=$((pruned + 1))
  say "      pruned $(basename "$stale")"
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${PG_DATABASE}_*.dump" \
  -mtime "+${BACKUP_RETENTION_DAYS}" -print)
say "      pruned $pruned dump(s)"

say "4/4 done."
say "RESULT: backup written to $target ($(wc -c <"$target" | tr -d ' ') bytes)."
trap - EXIT
