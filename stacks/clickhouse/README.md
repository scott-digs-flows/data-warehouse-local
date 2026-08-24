# ClickHouse stack

Self-contained. **One container**, no object store, no catalog, no other stack.

Data lives in ClickHouse's own MergeTree tables, loaded from the shared raw
layer (`shared/data` + `shared/schemas`).

## Run it

```bash
cp .env.example .env
docker compose up -d
uv run python load.py          # ~5s for 29 tables / 1.06M rows
```

Tear down with `docker compose down` (keeps data) or `down -v` (wipes it; reload
takes seconds).

## Connect

| Interface | Port | Use |
| --- | --- | --- |
| HTTP | 8123 | Python client, `curl`, clickhouse-jdbc |
| Native | **9010** | `clickhouse-client` — remapped off 9000 to avoid clashing with the Iceberg stack's MinIO |
| Postgres wire | 9005 | **GUI clients — use this for DBeaver** |

User `default`, password `clickhouse` (from `.env`).

```bash
# HTTP
curl -u default:clickhouse http://localhost:8123/ \
  --data-binary 'SELECT count() FROM raw.fact_internet_sales'

# psql / DBeaver — pick the PostgreSQL driver, port 9005
PGPASSWORD=clickhouse psql -h 127.0.0.1 -p 9005 -U default -d default \
  -c 'SELECT count(*) FROM raw.dim_customer'
```

### DBeaver

Use the **PostgreSQL** driver on port **9005**, not a ClickHouse driver.
DBeaver's ClickHouse drivers send `Authorization: Basic` *and*
`X-ClickHouse-User` headers together, which ClickHouse rejects outright:

```
Code: 516. Invalid authentication: it is not allowed to use X-ClickHouse HTTP
headers and Authorization HTTP header simultaneously.
```

There is no server-side setting to relax that (checked `system.settings` and
`system.server_settings`), and both the current and legacy ClickHouse drivers
do it. The Postgres wire protocol sidesteps the JDBC driver entirely.

Unlike the Iceberg stack, tables here are ordinary MergeTree tables, so they
register in `system.tables` and browse normally — no views needed.

## What gets loaded

29 tables in the `raw` database, 1,060,715 rows, **9.5 MiB on disk** (from
182 MB of CSV).

- Column types come from `shared/schemas/*.json`, not from CSV inference, so
  this stack agrees with every other stack about what the data is.
- Identifiers are snake_case (`DimCustomer.EnglishProductName` →
  `dim_customer.english_product_name`).
- `ORDER BY` uses the table's first non-nullable `*_key` column when there is
  one, else `tuple()`. That is a heuristic, not a tuned sorting key — worth
  revisiting before drawing performance conclusions, since `ORDER BY` is
  ClickHouse's main performance lever.
- Three `varbinary` photo columns and the `DatabaseLog` table are excluded; see
  `shared/scripts/common.py` for why.

## Ports

Chosen so this stack can run at the same time as `stacks/iceberg-multi-engine/`
without clashing. If you only ever run one stack, the defaults in `.env` are
fine as-is.
