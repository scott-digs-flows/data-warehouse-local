#!/bin/sh
# Bootstraps a fresh Lakekeeper instance and creates the warehouse.
#
# The warehouse payload is a template rendered with envsubst, so MinIO
# credentials live in .env only and never in a checked-in JSON file.
#
# Idempotent: both endpoints return 4xx once already bootstrapped, which we
# tolerate so `docker compose up` is safe to re-run.

set -eu

apk add --no-cache curl gettext >/dev/null 2>&1

LAKEKEEPER_URL="${LAKEKEEPER_URL:-http://lakekeeper:8181}"

echo "--> rendering warehouse payload for '${ICEBERG_WAREHOUSE}'"
envsubst < /create-warehouse.json.tmpl > /tmp/create-warehouse.json

echo "--> bootstrap"
curl -sS -X POST "${LAKEKEEPER_URL}/management/v1/bootstrap" \
    -H 'Content-Type: application/json' \
    -d '{"accept-terms-of-use": true}' \
    -w '\nHTTP %{http_code}\n' || true

echo "--> create warehouse ${ICEBERG_WAREHOUSE}"
curl -sS -X POST "${LAKEKEEPER_URL}/management/v1/warehouse" \
    -H 'Content-Type: application/json' \
    -d @/tmp/create-warehouse.json \
    -w '\nHTTP %{http_code}\n' || true

echo "--> done"
