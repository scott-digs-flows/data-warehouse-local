# data-warehouse-local

A local Iceberg lake with several query engines pointed at it, built to back a
**custom BI application developed in a separate project**.

Two questions drive this repo:

1. **Connector behaviour.** Give the BI app four real SQL endpoints — Trino,
   DuckDB, ClickHouse, Postgres — so its dialect layer, type handling, and
   metadata discovery get exercised against genuinely different engines.
2. **Iceberg as a neutral layer.** Can one lake serve every engine, instead of
   one ETL pipeline per engine? Partly — and the partial answer is the
   interesting result. See [Tiers](#engine-tiers).

Source data is Microsoft's **AdventureWorks DW 2022** sample: a real star
schema, small enough to reload in seconds. It is sized for fast app iteration,
**not** for performance measurement — every engine answers instantly at this
volume, so no conclusions about speed should be drawn here.

## Engine tiers

The central finding: Iceberg gets you a shared layer for *most* engines, and the
rest need a copy.

```
                        ┌──────────────────────┐
                        │   MinIO (S3)         │
                        └──────────┬───────────┘
                        ┌──────────┴───────────┐
                        │ Lakekeeper REST      │  warehouse: adventure_works_dw
                        │ Iceberg catalog      │  namespace: raw
                        └──────────┬───────────┘
                                   │
   ── Tier 1: read in place, zero copy ─────────────────────────────
                                   │
        ┌──────────────┬───────────┼──────────────┐
        │              │           │              │
   ┌────┴────┐   ┌─────┴─────┐ ┌───┴──────┐  ┌────┴──────────┐
   │  Trino  │   │  DuckDB   │ │ DuckDB   │  │  ClickHouse   │
   │  :8080  │   │  HTTP     │ │ embedded │  │  :8123        │
   │         │   │  :8000    │ │ in-proc  │  │               │
   └─────────┘   └───────────┘ └──────────┘  └───────────────┘

   ── Tier 2: requires a copy ──────────────────────────────────────
                                   │
                          sync_postgres.py
                                   │
                            ┌──────┴──────┐
                            │  Postgres   │  db: analytics
                            │  :5432      │  schema: raw
                            └─────────────┘
```

**Tier 1** engines query the same Iceberg tables directly. **Postgres has no
mature native Iceberg reader**, so it holds a synced copy — one extra job, one
more thing that can go stale. That is the concrete cost of the tradeoff, and
having exactly one copy-based engine keeps it easy to reason about.

| Component | Role | Image |
| --- | --- | --- |
| MinIO | S3-compatible object storage | `minio/minio:RELEASE.2025-09-07T16-13-09Z` |
| Postgres | Catalog metadata + `analytics` copy | `postgres:16` |
| Lakekeeper | Iceberg REST catalog | `quay.io/lakekeeper/catalog:v0.13.1` |
| Trino | Query engine | `trinodb/trino:466` |
| DuckDB API | FastAPI wrapper over DuckDB | `./services/duckdb-api` |
| ClickHouse | Query engine | `clickhouse/clickhouse-server:26.7.3.19` |

## Prerequisites

- Docker with Compose v2 (~8 GB allocated is plenty)
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- On Apple Silicon, SQL Server runs under amd64 emulation during extraction

**Required `/etc/hosts` entries:**

```
127.0.0.1  lakekeeper
127.0.0.1  minio
```

The REST catalog advertises its own URI and object-storage endpoint using
*internal Docker hostnames* (`http://lakekeeper:8181`, `http://minio:9000`).
Clients running on the host follow those names, so without these entries every
host-side script fails in confusing ways. This is the single most likely reason
a fresh checkout does not work.

## Quickstart

```bash
cp .env.example .env          # single source for all credentials and ports
uv sync --extra duckdb-api

docker compose up -d                     # lake: MinIO + Postgres + Lakekeeper
uv run python scripts/download_adventure_works.py   # ~10 min (SQL Server restore)
uv run python scripts/load_iceberg.py               # CSV -> Iceberg `raw`

docker compose --profile engines up -d   # Trino + DuckDB API + ClickHouse
uv run python scripts/sync_postgres.py   # Iceberg -> Postgres copy

uv run python scripts/smoke_test.py      # verify all engines agree
```

Expected final output:

```
✓ All engines agree on 60,398 rows
✓ All engines agree on the aggregate (10 territory groups, sums matched to 2dp)
```

### Running just one engine

Every engine has its own profile as well as `engines`, so you only pay for what
you use. The lake services carry no profile and always start.

```bash
docker compose --profile clickhouse up -d   # lake + ClickHouse only  (4 containers, ~1.0 GB)
docker compose --profile trino up -d        # lake + Trino only
docker compose --profile duckdb up -d       # lake + DuckDB HTTP only
docker compose --profile engines up -d      # lake + all three        (6 containers, ~2.1 GB)
```

Restrict the smoke test to match:

```bash
uv run python scripts/smoke_test.py --engine clickhouse
```

### Making ClickHouse browsable in a GUI

DataLakeCatalog tables never register in `system.tables`, so DBeaver and other
tools see `datalake` as empty. Create pass-through views once after loading:

```bash
uv run python scripts/create_clickhouse_views.py
```

That exposes all 29 tables as `raw.<table>` — visible to `system.tables` and
`information_schema`, with real column types, and without the
`datalake."raw.dim_customer"` quoting that GUIs mangle. No data is copied.

Postgres always runs — it stores the Iceberg catalog metadata regardless of
which engine you query. `sync_postgres.py` is only needed if you want the
`analytics` copy as well.

## The engine manifest

[`engines.yaml`](engines.yaml) is the contract between this repo and the BI app.
It carries, per engine: connection details, the SQLGlot/Ibis **dialect** name, a
**`qualify` template** for building table references, and identifier-case
behaviour. The app reads it and needs no hardcoded connection logic — adding an
engine is a compose service plus a manifest entry.

The `qualify` template exists because engines genuinely disagree about how an
Iceberg namespace surfaces:

| Engine | Reference for `fact_internet_sales` |
| --- | --- |
| Trino | `iceberg.raw."fact_internet_sales"` |
| DuckDB | `ice.raw."fact_internet_sales"` |
| ClickHouse | `datalake."raw.fact_internet_sales"` — namespace folded into the name |
| Postgres | `raw."fact_internet_sales"` |

[`scripts/smoke_test.py`](scripts/smoke_test.py) is the reference implementation
of consuming the manifest, and the best starting point for the app's connector
layer.

## Data conventions

- **Warehouse** `adventure_works_dw`, **namespace** `raw` (`marts` is reserved
  for pre-aggregated tables when raw star-schema queries get awkward to chart).
- **Identifiers are snake_case.** `DimCustomer.EnglishProductName` becomes
  `dim_customer.english_product_name`. Engines disagree on case folding — Trino
  lowercases, Postgres folds unquoted names, ClickHouse is case-sensitive,
  Snowflake uppercases — so normalising once at load time removes an entire
  class of cross-engine bug. The CSV extract keeps the original casing;
  normalisation is a documented transformation in `load_iceberg.py`.
- **Types are pinned, never inferred.** `download_adventure_works.py` writes
  authoritative column types from SQL Server's `INFORMATION_SCHEMA` to
  `schemas/*.json`, and the loader uses those. CSV type inference disagrees
  between readers, which would give each engine a subtly different view of the
  same data.
- **Tables are unpartitioned.** The largest is under a million rows.
- **Pinned `NOT NULL` is enforced, not just recorded.** 144 of 343 fields are
  Iceberg `required`, from the `nullable` flag in `shared/schemas/*.json`
  (DW-20). A load that would write a NULL into one fails loudly rather than
  widening the schema to fit. **Visible to the BI app**: those 144 columns
  surface as e.g. `Int32` in ClickHouse rather than `Nullable(Int32)` — the
  split is 199 Nullable / 144 not. `engines.yaml` is unchanged, so nothing in
  the app's connection logic breaks, but anything pattern-matching on
  ClickHouse type strings will see it.

### Deliberate exclusions

| Excluded | Why |
| --- | --- |
| `DatabaseLog` table | SQL Server DDL audit table, not part of the star schema. Its `XmlEvent` column has embedded newlines that `bcp` character format cannot round-trip — only 5 of 1,864 exported rows were structurally intact. |
| 3 `varbinary` columns | Product/employee/territory photos: ~18 MB across ~900 rows, no BI value, and they carry NUL bytes that Postgres `text` rejects. |

Both are enforced in [`scripts/_common.py`](scripts/_common.py). The download
script also verifies each CSV by parsing it and reports any ragged rows, rather
than trusting a raw line count.

## Querying each engine

```bash
# Trino
docker compose exec trino trino --catalog iceberg --schema raw \
  --execute 'SELECT COUNT(*) FROM "fact_internet_sales"'

# ClickHouse
docker exec clickhouse clickhouse-client \
  --query 'SELECT count() FROM datalake."raw.fact_internet_sales"'

# DuckDB over HTTP  (interactive docs at http://localhost:8000/docs)
curl -X POST http://localhost:8000/query -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT COUNT(*) FROM ice.raw.\"fact_internet_sales\""}'

# Postgres
docker exec postgres psql -U lakekeeper -d analytics \
  -c 'SELECT COUNT(*) FROM raw.fact_internet_sales'
```

## Gotchas worth knowing

These all cost real debugging time; they are recorded so they only cost it once.

- **DuckDB secrets are scoped to `s3://`.** The Iceberg extension fetches
  manifest `.avro` files over an `http://` URL, which falls outside a
  `CREATE SECRET` scope, so those requests go out unsigned and MinIO answers
  403 — while plain `s3://` reads succeed. Use the global `SET s3_*` settings
  instead (see `attach_sql` in `engines.yaml`).
- **MinIO intermittently 403s the first few object operations of a process**,
  then settles. Reproduced identically under s3fs and PyArrowFileIO, and under
  two MinIO releases. `with_s3_retry()` in `_common.py` absorbs it; only wrap
  genuinely idempotent operations.
- **Retrying a read only works if the retry re-loads the table.** Following on
  from the 403s above: Lakekeeper vends per-table S3 signing config, and
  PyIceberg binds it to the `Table` object at `load_table()` time. So retrying a
  *cached* table's `.scan()` never recovers — measured 15 consecutive 403s on
  one table object, while re-creating the catalog settled on the 3rd attempt.
  Keep `load_table()` **inside** the retried closure:
  ```python
  with_s3_retry(lambda: catalog.load_table((NAMESPACE, name)).scan().count(), name)
  ```
  `sync_postgres.py` already does this. Hoisting the `load_table()` out to avoid
  "redundant" work is what reintroduces the failure. Note the loader reports row
  counts from the in-memory Arrow table and never reads back, so a read-path
  breakage like this is invisible to a green `load_iceberg.py` run.
- **ClickHouse can write to Iceberg, but only just.** `INSERT` and
  `ALTER … DELETE` work with `allow_insert_into_iceberg=1` (beta/experimental
  gates) and produce genuine Iceberg snapshots — verified `append` then
  `overwrite` in the catalog, and **Trino reads the result correctly**. But
  **PyIceberg cannot read ClickHouse-written data files**, failing with
  `Cannot convert field, missing field-id`. `CREATE TABLE` into the catalog and
  `TRUNCATE` are not supported at all. Treat ClickHouse as read-only unless
  every reader in the loop is known to tolerate its output.
- **ClickHouse rejects two auth headers at once** — `Code: 516 … not allowed to
  use X-ClickHouse HTTP headers and Authorization HTTP header simultaneously`.
  DBeaver's modern ClickHouse driver (clickhouse-jdbc v2) authenticates with
  `X-ClickHouse-User/Key`; if the client also sends `Authorization: Basic`, the
  server refuses the pair, and there is **no server setting to relax it**
  (checked `system.settings` and `system.server_settings`). Options, in order:
  use DBeaver's **ClickHouse (Legacy)** driver, which sends Basic auth only;
  ensure credentials are supplied in exactly one place (not in both the auth
  fields and the JDBC URL); or connect with a **Postgres driver on port 9005**,
  where ClickHouse's Postgres wire emulation bypasses the JDBC driver entirely:
  ```bash
  PGPASSWORD=clickhouse psql -h 127.0.0.1 -p 9005 -U default -d default \
    -c 'SELECT count() FROM datalake."raw.dim_customer"'
  ```
- **ClickHouse won't use server-configured S3 credentials in user queries**
  unless `s3_allow_server_credentials_in_user_queries` is set. The alternative
  puts the MinIO key/secret into SQL and `SHOW CREATE DATABASE` output.
- **ClickHouse ships listening on `::1` only.** The container looks healthy —
  its healthcheck runs *inside* the container — while being unreachable from
  everywhere else. `config.d/network.xml` fixes it.
- **ClickHouse `initdb.d` scripts run only on a first-ever start.** One failed
  boot leaves the catalog permanently unattached with no obvious symptom, so
  catalog attachment is an idempotent one-shot compose service instead.
- **`network … not found` on `up`.** Containers store the network *ID* they were
  created with. If `warehouse-net` was recreated since — a `down` and later
  `up`, or a Docker restart — older containers still point at the dead ID, and
  Compose tries to *start* them rather than recreate them. Delete the stale ones
  and let Compose rebuild:
  ```bash
  docker rm -f trino duckdb-api clickhouse clickhouse-init
  docker compose --profile engines up -d
  ```
  Volumes are untouched, so the lake survives. `docker compose --profile all
  down` before bringing the stack back up avoids it entirely.
- **JSON over HTTP loses numeric types.** The DuckDB HTTP endpoint returns
  `SUM(...)` as the string `"3649866.5512"`. A BI app on a JSON transport has to
  re-infer types a native driver would have preserved.
- **287 `DimProduct` name values arrive as a literal NUL byte**, not an empty
  field. Parquet and Iceberg carry them fine; Postgres `text` rejects them, so
  they are cleaned at the load boundary and every engine sees the same data.
  These are *not* NULLs that `bcp -k` mangled — both columns are pinned
  `nullable=false`, so they are literal NCHAR(0) values meaning "no translation
  available". They become **the empty string** (DW-19, decided 2026-09-11); NULL
  was the old behaviour and asserted "unknown" where the source said "empty".
  See `DECISIONS.md`.
- **`pyarrow.csv` nulls the literal string `NA` unless you stop it.** Fixed
  under **DW-18**; do not undo it. PyArrow defaults `null_values` to a 17-token
  list including `NA`, `N/A`, `null`, `NaN` and `#N/A`, and silently nulls any
  value matching one. AdventureWorks uses `NA` as a real value, so this cost
  **564 values across 5 columns** — `dim_product.color` (254),
  `dim_product.size_range` (307), and all three text columns of
  `dim_sales_territory` row 11, the star schema's unknown member. The loader now
  passes an explicit `null_values=[""]`, because an empty field is the only
  thing `bcp` writes for NULL. **Pinning *types* does not pin *which values mean
  NULL*** — that is a second, separate place inference can creep in, and it is
  invisible in a green load. An audit of all 16 non-empty tokens across all 29
  tables found only `NA` live today, but the fix is deliberately exhaustive so a
  future source containing `null` or `NaN` as real text does not reintroduce it.
- **NULL and the empty string are indistinguishable in the extract.** `bcp`
  character format writes an unquoted empty field for both, so the distinction
  is gone before the loader runs — no loader change can recover it. **Accepted
  deliberately** (DW-19, `DECISIONS.md`) rather than re-extracting with a NULL
  sentinel: 191,778 values across 32 nullable columns are formally ambiguous,
  but "no value" is one concept in a BI star schema. The lake's only 574 empty
  strings are the NCHAR(0) case above, which is why they are worth knowing about
  — every other empty source field is a NULL here.

## Directory layout

```
data-warehouse-local/
├── engines.yaml                  # contract consumed by the BI app
├── docker-compose.yml
├── schemas/                      # pinned column types (checked in)
├── scripts/
│   ├── download_adventure_works.py   # bak -> SQL Server -> bcp -> CSV + schemas
│   ├── load_iceberg.py               # CSV -> Iceberg `raw`  (canonical loader)
│   ├── sync_postgres.py              # Iceberg -> Postgres   (full refresh)
│   ├── smoke_test.py                 # query every engine, compare results
│   ├── _common.py                    # config, snake_case, retry helper
│   └── _manifest.py                  # engines.yaml reader
├── services/
│   ├── clickhouse/{config.d,users.d,attach-catalog.sh}
│   ├── duckdb-api/                   # FastAPI service
│   ├── lakekeeper/                   # bootstrap + warehouse template
│   ├── postgres/init-databases.sql
│   └── trino/catalog/iceberg.properties
├── data/                         # extracted CSVs (gitignored)
└── experiments/loader-comparison/    # parked: PyIceberg vs DuckDB vs DataFusion
```

## Deferred, deliberately

- **Snowflake** — supports Iceberg, but requires real cloud object storage and
  cannot read local MinIO. When needed, either point it at an S3 bucket or just
  `COPY INTO` a trial account; for testing the app's connector, the data's
  origin does not matter.
- **StarRocks** — the most interesting engine for a BI workload (materialised
  views over Iceberg with transparent query rewrite), but that advantage is
  invisible at this data volume, where it is merely redundant with Trino.
- **Druid** — dropped. It cannot read Iceberg; it copies data into its own
  segment format, which made it an ETL target rather than an engine, at a cost
  of six containers and most of the RAM budget.
- **Incremental sync** — Postgres is a full refresh. The dataset reloads in
  seconds, and Iceberg snapshots keep the incremental path open for later.
