#!/bin/sh
# Bootstraps a fresh Lakekeeper instance and creates the adventure_works_dw warehouse.
# Idempotent — both endpoints return 4xx on re-run and we ignore those.

set -eu

LAKEKEEPER_URL="${LAKEKEEPER_URL:-http://lakekeeper:8181}"

echo "--> bootstrap"
curl -sS -X POST "${LAKEKEEPER_URL}/management/v1/bootstrap" \
    -H 'Content-Type: application/json' \
    -d '{"accept-terms-of-use": true}' \
    -w '\nHTTP %{http_code}\n' || true

echo "--> create warehouse adventure_works_dw"
curl -sS -X POST "${LAKEKEEPER_URL}/management/v1/warehouse" \
    -H 'Content-Type: application/json' \
    -d @/create-warehouse.json \
    -w '\nHTTP %{http_code}\n' || true

echo "--> done"
