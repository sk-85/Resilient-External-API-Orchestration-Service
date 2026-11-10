#!/usr/bin/env bash
# Walks through the resilience behaviour. Needs the service on :8000 and the mock upstream on :9000.
#   terminal 1: python3 mock_external/server.py
#   terminal 2: uvicorn app.main:create_app --factory
#   terminal 3: bash scripts/demo.sh
set -u

API="${API:-http://localhost:8000}"
MOCK="${MOCK:-http://localhost:9000}"
AUTH=()
[ -n "${API_KEY:-}" ] && AUTH=(-H "X-API-Key: ${API_KEY}")

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

call() {  # call <path> [params-json]  -> prints a compact summary of the orchestration result
  local params="${2:-null}"
  curl -s -w '\nHTTP %{http_code}\n' ${AUTH[@]+"${AUTH[@]}"} -X POST "$API/v1/orchestrations" \
    -H 'Content-Type: application/json' -d "{\"path\": \"$1\", \"params\": $params}" \
    | python3 "$HERE/summarize.py"
}

step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

curl -s "$MOCK/admin/reset" >/dev/null

step "1. Healthy upstream: succeeds on the first attempt"
call "/ok"

step "2. Upstream fails twice, then recovers: retried with backoff, then succeeds (attempts=3)"
call "/fail-then-succeed" '{"fails": 2, "key": "demo1"}'

step "3. Upstream rate-limits us (429 + Retry-After): we wait as told, then succeed"
call "/rate-limited" '{"limit": 2, "key": "demo2"}'

step "4. Upstream says 404: a client error is NOT retried (attempts=1)"
call "/not-found"

step "5. Upstream is permanently broken: retries exhausted, then the circuit opens"
call "/always-500"
call "/always-500"
echo "  circuit state:"
curl -s ${AUTH[@]+"${AUTH[@]}"} "$API/v1/circuit" | python3 -m json.tool | sed 's/^/    /'

step "6. While the circuit is open, calls fail fast without touching the upstream"
call "/ok"

echo
echo "Next: wait ~15s (ORCH_BREAKER_RECOVERY_SECONDS) and call /ok again."
echo "The breaker lets one probe through (half-open); on success it closes."
echo
echo "Inspect the audit trail of any run:  GET $API/v1/orchestrations  then  /v1/orchestrations/<id>"
echo "Metrics:                             GET $API/metrics"
