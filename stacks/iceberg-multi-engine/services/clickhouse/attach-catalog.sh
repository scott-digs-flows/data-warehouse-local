#!/bin/bash
# Attach the Lakekeeper REST catalog to ClickHouse as database `datalake`.
#
# Runs as a one-shot compose service on every `up`, not via initdb.d — that
# only fires on a first-ever start, so a single failed boot would leave the
# catalog permanently unattached with no obvious symptom.
#
# CREATE DATABASE IF NOT EXISTS makes this safe to re-run.
#
# Iceberg tables then appear as `datalake."raw.<table>"` — ClickHouse flattens
# the Iceberg namespace into the table name rather than nesting it.

set -euo pipefail

echo "--> attaching Iceberg catalog '${ICEBERG_WAREHOUSE}' to ClickHouse"

clickhouse-client --host "${CLICKHOUSE_HOST:-clickhouse}" \
    --user "${CLICKHOUSE_USER:-default}" --password "${CLICKHOUSE_PASSWORD}" -n --query "
    SET allow_experimental_database_iceberg = 1;

    CREATE DATABASE IF NOT EXISTS datalake
    ENGINE = DataLakeCatalog('${ICEBERG_CATALOG_URI}')
    SETTINGS
        catalog_type = 'rest',
        warehouse = '${ICEBERG_WAREHOUSE}',
        storage_endpoint = '${MINIO_ENDPOINT}/${MINIO_BUCKET}';
"

echo "--> catalog attached as database 'datalake'"
