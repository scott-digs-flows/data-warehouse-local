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
# HTTP — credentials are required; without -u this fails with
# "Code: 194 ... Authentication failed". The password is deliberately
# non-blank (see the Code 516 double-auth trap below), so there is no
# credential-free HTTP route.
curl -u default:clickhouse \
  "http://localhost:8123/?query=SELECT%20count()%20FROM%20raw.dim_customer"

# clickhouse-client in the container
docker exec clickhouse clickhouse-client \
  --query 'SELECT count() FROM raw.dim_customer'

# Postgres wire protocol — the reliable route for GUIs
PGPASSWORD=clickhouse psql -h 127.0.0.1 -p 9005 -U default -d default \
  -c 'SELECT count() FROM raw.dim_customer'
```

Ports: HTTP `8123`, native `9010` (moved off `9000` because MinIO owns it),
Postgres wire `9005`.

**Which route for a GUI: HTTP 8123 with a ClickHouse driver.** Database `raw`,
user `default`, password `clickhouse`, credentials in exactly one place.
Measured under DW-13: over HTTP, `system.tables` / `system.columns` /
`information_schema` all report 29 tables and 343 columns for `raw`, and
`DESCRIBE TABLE raw.dim_customer` returns its 29 columns — everything a
navigator needs.

**Not the Postgres driver on 9005, despite it being the easier connection.**
It executes SQL correctly and the navigator still stays empty, for two
independent reasons:

- `pg_catalog` is not a database here. Qualified `pg_catalog.pg_class` answers
  `Database pg_catalog does not exist`. Unqualified `pg_class` / `pg_namespace`
  / `pg_attribute` / `pg_type` do resolve, but as content-free stubs —
  `pg_class` has no `relname` — and drivers schema-qualify, so they never reach
  them.
- **The parser rejects the SQL these drivers emit anyway**: bare `~`, `!~`,
  `OPERATOR(pg_catalog.~)` and `E'...'` literals are syntax errors. Populating
  `pg_catalog` later would not fix it. This is the more durable reason.

Verified against pgjdbc 42.7.4 via `DatabaseMetaData`: `getSchemas`,
`getTables`, `getColumns`, `getCatalogs` all fail; a plain query returns 18,484.

Use 9005 as a query fallback, not to browse — and note a JDBC client needs
`?sslmode=disable`, because ClickHouse answers the Postgres `SSLRequest` with
`S` then drops the handshake, so the default `sslmode=prefer` fails at connect.
Any error also kills the pgwire session.

## Failure modes

Each of these cost real debugging time. Recognise them rather than rediscover
them.

**`Code: 516 … not allowed to use X-ClickHouse HTTP headers and Authorization
HTTP header simultaneously`** — ClickHouse refuses two auth headers at once.
DBeaver's modern driver (clickhouse-jdbc v2) sends `X-ClickHouse-User/Key`; if
the client also sends `Authorization: Basic`, the server rejects the pair, and
**there is no server setting to relax it** (checked `system.settings` and
`system.server_settings`). Still live on 26.7.3.19: both headers → 516, either
one alone → fine.

The fix is **credentials in exactly one place**, not a different transport.
Basic alone works; `X-ClickHouse-*` alone works. The pair is the only failure,
and it usually comes from filling in the auth fields *and* the JDBC URL.

**This skill used to rank a Postgres driver on 9005 first. That was wrong for
browsing** and is corrected under DW-13 — see *Connecting* above. 9005 is a
fine query fallback and a genuine way around the 516 trap, but pgwire has no
`pg_catalog`, so a navigator stays empty.

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

What a BI tool actually sees in the `raw` views, i.e. source → Iceberg →
ClickHouse. Verified column-by-column across all 343 columns under DW-12:

| Source | Iceberg | ClickHouse view |
| --- | --- | --- |
| `bit` | `boolean` | `Bool` |
| `tinyint` / `smallint` / `int` | `int` | **`Int32` — all three** |
| `bigint` | `long` | `Int64` |
| `real` / `float` | `float` / `double` | `Float32` / `Float64` |
| `decimal`, `numeric` | `decimal(p, s)` | `Decimal(p, s)` |
| `money` / `smallmoney` | `decimal(19,4)` / `decimal(10,4)` | `Decimal(19, 4)` / `Decimal(10, 4)` |
| `date` | `date` | `Date32` |
| `datetime*` | `timestamp` (µs) | `DateTime64(6)` |
| `time` | `time` | **untested — see below** |

Columns the pinned schema marks nullable wrap in `Nullable(...)`; the 144 marked
`NOT NULL` do not (DW-20).

**The integer row is the one that surprises people.** Iceberg has no 8- or
16-bit integer, so `tinyint` and `smallint` widen to `int` on the way in and
reach ClickHouse as `Int32`. Measured: all 128 integer columns in the lake are
`Int32`, with no `UInt8`, `Int16` or `Int64` anywhere.

> An earlier version of this table described `stacks/clickhouse/load.py`'s
> `type_pair()` — the **off-pipeline** CSV→MergeTree loader that DW-15 removes —
> and claimed `tinyint`/`smallint`/`bigint` reach ClickHouse as
> `UInt8`/`Int16`/`Int64`. That is true of that loader and false of the Iceberg
> path this skill is about. Corrected under DW-12. The same applies to the
> `ORDER BY` note that used to sit here: views have no sorting key, so it
> described the MergeTree path too.

**No type collapses unexpectedly.** Across all 343 columns, 0 differ from the
pinned schema and 0 non-string source type surfaces as `String`.

**The `time` row is genuinely unknown, and the old claim about it was wrong.**
AdventureWorks DW contains **no `time` columns at all** (census: `nvarchar` 132,
`int` 90, `money` 25, `tinyint` 23, `datetime` 17, `smallint` 15, `nchar` 12,
`float` 10, `date` 9, `bit` 6, `real` 2, `char` 1, `varchar` 1), so nothing here
exercises it. This skill used to assert `time` becomes `String` "because
ClickHouse has no time-of-day type" — that reason is false on the version this
stack runs:

```
$ docker exec clickhouse clickhouse-client --query \
    "SELECT name FROM system.data_type_families WHERE name ILIKE 'time%'"
TIMESTAMP
Time
Time64
$ ... --query "SELECT toTypeName(toTime64('12:34:56.789', 3))"
Time64(3)
```

So a future source with `time` columns is at least as likely to surface as
`Time64` as `String`. Treat the landing type as unmeasured until a dataset with
`time` columns actually loads. Verified on ClickHouse 26.7.3.19 under DW-12.

## The engines.yaml contract

`engines.yaml` is how the BI app learns to reach every engine: connection
details, the SQLGlot/Ibis `dialect` string, the `qualify` template, and
identifier-case behaviour. `scripts/smoke_test.py` is the reference
implementation of consuming it, and the best starting point for the app's
connector layer.

**Adding an engine is a compose service plus a manifest entry — never app code
changes.** Changing the manifest's shape is a breaking change for a consumer
you cannot see from this repo; label such tickets `contract`.
