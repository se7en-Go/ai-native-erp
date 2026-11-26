#!/usr/bin/env bash
# Track 8 / Ops — one-shot production healthcheck.
#
# Verifies every externally observable surface of the stack and exits non-zero
# on the first-class failure report so a cron/Nagios/K8s probe can alert on it:
#
#   backend  GET /healthz   (liveness)
#   backend  GET /readyz    (Postgres + Redis readiness, 503 when degraded)
#   backend  GET /metrics   (Prometheus exposition, text/plain; version=0.0.4)
#   frontend GET /login     (Next.js workbench shell)
#   postgres pg_isready
#   redis    PING
#
# Overridable environment:
#   BACKEND_URL     default http://127.0.0.1:8000
#   WEB_URL         default http://127.0.0.1:3000
#   PG_CONTAINER    default erp_prod_postgres   ("" disables the docker exec path)
#   REDIS_CONTAINER default erp_prod_redis
#   PG_USER         default postgres
#   PG_DATABASE     default erp_saas
#   PGHOST/PGPORT   used when PG_CONTAINER is empty
#   REDIS_URL       used when REDIS_CONTAINER is empty
#   CURL_TIMEOUT    default 5 (seconds)
set -euo pipefail

BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8000}"
WEB_URL="${WEB_URL:-http://127.0.0.1:3000}"
PG_CONTAINER="${PG_CONTAINER-erp_prod_postgres}"
REDIS_CONTAINER="${REDIS_CONTAINER-erp_prod_redis}"
PG_USER="${PG_USER:-postgres}"
PG_DATABASE="${PG_DATABASE:-erp_saas}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"

CHECKS=0
FAILURES=0

say() { printf '%s\n' "$*"; }
pass() { CHECKS=$((CHECKS + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { CHECKS=$((CHECKS + 1)); FAILURES=$((FAILURES + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$*"; }

# http_status <url> -> prints the HTTP status code, or 000 when unreachable.
http_status() {
  curl -sS -o /dev/null -m "$CURL_TIMEOUT" -w '%{http_code}' "$1" 2>/dev/null || printf '000'
}

check_http_ok() {
  local name="$1" url="$2" status
  status="$(http_status "$url")"
  if [ "$status" = "200" ]; then
    pass "$name -> HTTP 200 ($url)"
  else
    fail "$name -> HTTP $status ($url)"
  fi
}

check_http_body() {
  local name="$1" url="$2" needle="$3" body
  if body="$(curl -fsS -m "$CURL_TIMEOUT" "$url" 2>/dev/null)" && printf '%s' "$body" | grep -q "$needle"; then
    pass "$name -> contains '$needle' ($url)"
  else
    fail "$name -> missing '$needle' or unreachable ($url)"
  fi
}

check_postgres() {
  if [ -n "$PG_CONTAINER" ] && command -v docker >/dev/null 2>&1; then
    if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DATABASE" >/dev/null 2>&1; then
      pass "postgres -> pg_isready ok (container $PG_CONTAINER db $PG_DATABASE)"
      return
    fi
    fail "postgres -> pg_isready failed (container $PG_CONTAINER db $PG_DATABASE)"
    return
  fi
  if command -v pg_isready >/dev/null 2>&1; then
    if pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PG_USER" -d "$PG_DATABASE" >/dev/null 2>&1; then
      pass "postgres -> pg_isready ok ($PGHOST:$PGPORT db $PG_DATABASE)"
      return
    fi
    fail "postgres -> pg_isready failed ($PGHOST:$PGPORT db $PG_DATABASE)"
    return
  fi
  skip "postgres -> neither docker nor pg_isready is available"
}

check_redis() {
  if [ -n "$REDIS_CONTAINER" ] && command -v docker >/dev/null 2>&1; then
    if [ "$(docker exec "$REDIS_CONTAINER" redis-cli PING 2>/dev/null | tr -d '\r')" = "PONG" ]; then
      pass "redis -> PONG (container $REDIS_CONTAINER)"
      return
    fi
    fail "redis -> PING failed (container $REDIS_CONTAINER)"
    return
  fi
  if command -v redis-cli >/dev/null 2>&1; then
    if [ "$(redis-cli -u "$REDIS_URL" PING 2>/dev/null | tr -d '\r')" = "PONG" ]; then
      pass "redis -> PONG ($REDIS_URL)"
      return
    fi
    fail "redis -> PING failed ($REDIS_URL)"
    return
  fi
  skip "redis -> neither docker nor redis-cli is available"
}

say "== ERP production healthcheck =="
say "backend:  $BACKEND_URL"
say "frontend: $WEB_URL"
say ""

check_http_ok "backend liveness" "$BACKEND_URL/healthz"
check_http_ok "backend readiness" "$BACKEND_URL/readyz"
check_http_body "backend metrics" "$BACKEND_URL/metrics" "http_requests_total"
check_http_ok "frontend login shell" "$WEB_URL/login"
check_postgres
check_redis

say ""
if [ "$FAILURES" -gt 0 ]; then
  say "RESULT: $FAILURES of $CHECKS check(s) FAILED."
  exit 1
fi
say "RESULT: all $CHECKS checks passed."
