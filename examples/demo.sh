#!/usr/bin/env bash
# End-to-end demonstration against a running API (default http://localhost:8000).
#
# The script works whether the background worker is enabled or not: after
# creating a task it inspects the still-old attribution, then drives the task
# with /advance (concurrency-safe with the worker) and polls to completion.
# For a fully deterministic view of the "old attribution is served until the
# switch" step run the API with WORKER_ENABLED=false; the script then performs
# all backfill work itself.
#
# Usage: ./examples/demo.sh [BASE_URL]
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
STORE="DEMO-001"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
post() { curl -sS -X POST "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2" -w '\nHTTP %{http_code}\n'; }
put()  { curl -sS -X PUT  "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2" -w '\nHTTP %{http_code}\n'; }
get()  { curl -fsS "$BASE_URL$1"; }
first_line() { head -n 1; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }

sales_summary() {
  curl -fsS "$BASE_URL/api/v1/sales?store_id=$STORE" \
    | python3 -c 'import json, sys
for item in json.load(sys.stdin)["items"]:
    print("  {serial_no}: {region_code} ({attribution_status})".format(**item))'
}

table_summary() {
  curl -fsS "$BASE_URL/api/v1/stores/$STORE/region-table" \
    | python3 -c 'import json, sys
d = json.load(sys.stdin)
print("  table_id={} periods={}".format(d["table_id"], [p["region_code"] for p in d["periods"]]))'
}

# Advance a task and poll until it commits (or the worker commits it first).
drive() {
  local task_id="$1" status
  for _ in $(seq 1 200); do
    status="$(get "/api/v1/backfill-tasks/$task_id" | field status)"
    [ "$status" = "completed" ] && break
    curl -sS -X POST "$BASE_URL/api/v1/backfill-tasks/$task_id/advance?max_batches=1000" >/dev/null || true
    sleep 0.05
  done
  get "/api/v1/backfill-tasks/$task_id"
}

say "1. Import sales BEFORE any region table exists -> all pending"
post /api/v1/sales "{\"sales\":[
  {\"serial_no\":\"TX-1001\",\"store_id\":\"$STORE\",\"business_date\":\"2024-01-10\",\"amount\":\"120.00\"},
  {\"serial_no\":\"TX-1002\",\"store_id\":\"$STORE\",\"business_date\":\"2024-02-10\",\"amount\":\"80.50\"},
  {\"serial_no\":\"TX-1003\",\"store_id\":\"$STORE\",\"business_date\":\"2024-05-10\",\"amount\":\"45.00\"}
]}"

say "2. Publish the store's first complete region table (starts a backfill task)"
FIRST="$(put "/api/v1/stores/$STORE/region-table" "{\"store_id\":\"$STORE\",\"periods\":[
  {\"region_code\":\"NORTH\",\"start_date\":\"2024-01-01\",\"end_date\":\"2024-03-01\"},
  {\"region_code\":\"SOUTH\",\"start_date\":\"2024-03-01\",\"end_date\":\"2024-05-01\"}
]}" | first_line)"
echo "$FIRST"
FIRST_TASK="$(printf '%s' "$FIRST" | field task_id)"

say "3. Task progress as created"
get "/api/v1/backfill-tasks/$FIRST_TASK"

say "4. Drive the first backfill to completion, then read attributions"
drive "$FIRST_TASK"
echo
sales_summary
echo "  (TX-1003 on 2024-05-10 has no matching period -> pending)"

say "5. Idempotent re-import: TX-1001 identical (duplicate), TX-1002 altered (rejected)"
post /api/v1/sales "{\"sales\":[
  {\"serial_no\":\"TX-1001\",\"store_id\":\"$STORE\",\"business_date\":\"2024-01-10\",\"amount\":\"120.00\"},
  {\"serial_no\":\"TX-1002\",\"store_id\":\"$STORE\",\"business_date\":\"2024-02-10\",\"amount\":\"99.99\"}
]}"

say "6. Corrected region table: rename regions, extend coverage through 2024-05-10"
SECOND="$(put "/api/v1/stores/$STORE/region-table" "{\"store_id\":\"$STORE\",\"periods\":[
  {\"region_code\":\"NORTH-ZONE\",\"start_date\":\"2024-01-01\",\"end_date\":\"2024-03-01\"},
  {\"region_code\":\"SOUTH-ZONE\",\"start_date\":\"2024-03-01\",\"end_date\":\"2024-06-01\"}
]}" | first_line)"
echo "$SECOND"
SECOND_TASK="$(printf '%s' "$SECOND" | field task_id)"

say "7. While the task is unfinished: new imports succeed; queries still serve OLD attribution"
post /api/v1/sales "{\"sales\":[
  {\"serial_no\":\"TX-1004\",\"store_id\":\"$STORE\",\"business_date\":\"2024-02-20\",\"amount\":\"33.00\"}
]}"
echo
echo "active region table:"
table_summary
echo "sales:"
sales_summary

say "8. A second replacement is refused while the task is unfinished (expect 409)"
put "/api/v1/stores/$STORE/region-table" "{\"store_id\":\"$STORE\",\"periods\":[
  {\"region_code\":\"X\",\"start_date\":\"2024-01-01\",\"end_date\":\"2024-12-31\"}
]}"
echo "(with WORKER_ENABLED=true the worker may have committed the task already, in which case this returns 202)"

say "9. Drive the replacement to completion, then read the switched attributions"
drive "$SECOND_TASK"
echo
echo "active region table:"
table_summary
echo "sales:"
sales_summary

say "Done"
