---
name: clickhouse-warehouse
description: Serving Iceberg tables through ClickHouse — catalog attachment, making tables visible to BI tools and introspection, connecting clients, the engines.yaml contract, and the documented failure modes. Use when wiring, configuring, querying, or debugging the ClickHouse warehouse layer, or when adding another engine on the same pattern.
---

# Iceberg → ClickHouse

The second half of the pipeline. ClickHouse is the current warehouse engine; it
reads Iceberg, it does not own the data. Everything here should generalise to
the next engine.

## How ClickHouse reaches Iceberg

Two mechanisms, and the difference matters:

1. **`DataLakeCatalog` database** — attaches the Lakekeeper REST catalog so
   tables resolve by name. This is the wiring the stack uses.
2. **The `iceberg()` table function** — reads a raw S3 path with **no catalog
   at all**. Worth remembering: the minimum viable Iceberg layer is object
   storage alone, not object storage plus a catalog service. Useful for
   debugging whether a problem is in the catalog or the data.

## The visibility problem — and the fix

**`DataLakeCatalog` resolves tables lazily and never registers them in
`system.tables` or `system.columns`.** `SHOW TABLES FROM datalake` lists them,
but anything that introspects the system tables — DBeaver, most BI tools,
ClickHouse's own `information_schema` — sees an empty database. For a project
whose whole purpose is serving a BI app, that is fatal.

The fix is pass-through views, created by
`scripts/create_clickhouse_views.py`:

```bash
uv run python scripts/create_clickhouse_views.py
uv run python scripts/create_clickhouse_views.py --drop
```

Views **are** registered, so tools browse them with real column types and
autocomplete. No data is copied.

It also removes an awkward quoting rule. ClickHouse folds the Iceberg namespace
into the table name, making the direct reference
`datalake."raw.dim_customer"` — a dot inside a quoted identifier, which GUIs
routinely mangle. The views live in a `raw` database, so the reference becomes
`raw.dim_customer`, identical in shape to every other engine.

**Run this after every load, and after adding a new namespace.** A new dataset
namespace needs its own view database following the same pattern.

## Table reference shapes

Engines genuinely disagree about how an Iceberg namespace surfaces. This is why
`engines.yaml` carries a `qualify` template per engine:

| Engine | Reference for `fact_internet_sales` |
| --- | --- |
| Trino | `iceberg.raw."fact_internet_sales"` |
| DuckDB | `ice.raw."fact_internet_sales"` |
| ClickHouse | `datalake."raw.fact_internet_sales"` — namespace folded in |
| ClickHouse (views) | `raw.fact_internet_sales` — what BI tools should use |
| Postgres | `raw."fact_internet_sales"` |

## Connecting

```bash
# HTTP
curl "http://localhost:8123/?query=SELECT%20count()%20FROM%20raw.dim_customer"

# clickhouse-client in the container
docker exec clickhouse clickhouse-client \
  --query 'SELECT count() FROM raw.dim_customer'

# Postgres wire protocol — the reliable route for GUIs
PGPASSWORD=clickhouse psql -h 127.0.0.1 -p 9005 -U default -d default \
  -c 'SELECT count() FROM raw.dim_customer'
```

Ports: HTTP `8123`, native `9010` (moved off `9000` because MinIO owns it),
Postgres wire `9005`.

## Failure modes

Each of these cost real debugging time. Recognise them rather than rediscover
them.

**`Code: 516 … not allowed to use X-ClickHouse HTTP headers and Authorization
HTTP header simultaneously`** — ClickHouse refuses two auth headers at once.
DBeaver's modern driver (clickhouse-jdbc v2) sends `X-ClickHouse-User/Key`; if
the client also sends `Authorization: Basic`, the server rejects the pair, and
**there is no server setting to relax it** (checked `system.settings` and
`system.server_settings`). In order of preference: connect with a **Postgres
driver on port 9005**, bypassing the JDBC driver entirely; or use DBeaver's
**ClickHouse (Legacy)** driver, which sends Basic auth only; or ensure
credentials appear in exactly one place, not both the auth fields and the JDBC
URL.

**ClickHouse ships listening on `::1` only.** The container looks healthy — its
healthcheck runs *inside* the container — while being unreachable from
everywhere else. `config.d/network.xml` fixes it. Suspect this whenever the
container is healthy but the host cannot connect.

**`initdb.d` scripts run only on a first-ever start.** One failed boot leaves
the catalog permanently unattached with no obvious symptom. Catalog attachment
is therefore an idempotent one-shot compose service, not an init script. Keep
it that way.

**Server-configured S3 credentials are not used in user queries** unless
`s3_allow_server_credentials_in_user_queries` is set. The alternative puts the
MinIO key and secret into SQL — and into `SHOW CREATE DATABASE` output.

**ClickHouse can write to Iceberg, but only just.** `INSERT` and
`ALTER … DELETE` work behind `allow_insert_into_iceberg=1` and produce genuine
snapshots that **Trino reads correctly** — but **PyIceberg cannot read
ClickHouse-written data files** (`Cannot convert field, missing field-id`).
`CREATE TABLE` into the catalog and `TRUNCATE` are unsupported.
**Treat ClickHouse as read-only.** Writes belong in the ingestion layer, which
is the architecture anyway.

## Type notes

From the pinned schema, via `load.py`'s `type_pair()`:

| Source | ClickHouse |
| --- | --- |
| `bit` | `Bool` |
| `tinyint` / `smallint` / `int` / `bigint` | `UInt8` / `Int16` / `Int32` / `Int64` |
| `decimal`, `numeric` | `Decimal(p, s)` |
| `money` / `smallmoney` | `Decimal(19, 4)` / `Decimal(10, 4)` |
| `date` | `Date32` |
| `datetime*` | `DateTime64(6)` |
| `time` | `String` — **ClickHouse has no native time-of-day type** |

Nullable source columns wrap in `Nullable(...)`.

**`ORDER BY` is ClickHouse's main performance lever** and the current default is
untuned: the loader picks the first non-nullable `*_key` column, so
`fact_internet_sales` sorts by `product_key`. That is a defensible default, not
a tuned sorting key. Revisit it before any performance work — but note the
dataset is too small for performance work to mean anything.

## The engines.yaml contract

`engines.yaml` is how the BI app learns to reach every engine: connection
details, the SQLGlot/Ibis `dialect` string, the `qualify` template, and
identifier-case behaviour. `scripts/smoke_test.py` is the reference
implementation of consuming it, and the best starting point for the app's
connector layer.

**Adding an engine is a compose service plus a manifest entry — never app code
changes.** Changing the manifest's shape is a breaking change for a consumer
you cannot see from this repo; label such tickets `contract`.
