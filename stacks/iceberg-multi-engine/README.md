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

Checkout to querying ClickHouse, in one sequence. **Run from this directory**
(`stacks/iceberg-multi-engine/`) unless a line says otherwise. Verified end to
end from a destroyed stack under DW-14 — every step below was executed in this
order, and the two steps that used to be missing are marked.

```bash
# Start from a checkout.
git clone <this-repo> data-warehouse-local
cd data-warehouse-local/stacks/iceberg-multi-engine

# 0. Prerequisite, once per machine. Without these, every host-side script
#    fails in confusing ways — the catalog advertises Docker-internal hostnames.
#    See "Required /etc/hosts entries" above.
grep -qE '^127\.0\.0\.1\s+lakekeeper' /etc/hosts || \
  echo '127.0.0.1  lakekeeper' | sudo tee -a /etc/hosts
grep -qE '^127\.0\.0\.1\s+minio' /etc/hosts || \
  echo '127.0.0.1  minio' | sudo tee -a /etc/hosts

# 1. One-time: extract the source data. Restores a .bak into SQL Server, which
#    runs under amd64 emulation on Apple Silicon. Run from the REPO ROOT. Skip
#    if shared/data/adventure_works_dw/ is already populated.
#    ~10 min cold, but that is almost entirely a 1.68 GB image pull and a 97 MB
#    .bak download — measured at 33 s once both are cached (DW-8).
cd ../..
uv run python shared/scripts/extract_source.py
cd stacks/iceberg-multi-engine

# 2. Config and deps
cp .env.example .env          # single source for all credentials and ports
uv sync                       # add --extra duckdb-api only for the DuckDB HTTP service

# 3. Lake + ClickHouse. The catalog attaches itself; there is no manual step.
#    Do NOT add --wait here: Compose treats every exited container as a failure
#    regardless of exit code, so --wait returns 1 on a perfectly good start
#    ("container clickhouse-init exited (0)"). Check the attach explicitly:
docker compose --profile clickhouse up -d
docker logs clickhouse-init      # want: catalog attached as database 'datalake'

# 4. Raw CSVs -> Iceberg `raw`
uv run python scripts/load_iceberg.py

# 5. REQUIRED, and easy to miss: expose the Iceberg tables as ClickHouse views.
#    DataLakeCatalog tables never register in system.tables, and engines.yaml's
#    ClickHouse `qualify` is raw."{table}" — so without this, step 6 fails with
#    `Code: 81 ... Database raw does not exist`.
uv run python scripts/create_clickhouse_views.py

# 6. Verify
uv run python scripts/smoke_test.py --engine clickhouse
```

Expected final output:

```
✓ All engines agree on 60,398 rows
✓ All engines agree on the aggregate (10 territory groups, sums matched to 2dp)
```

**Runtime.** Steps 2–6 take **about 45 s, and up to a minute** from a fully
destroyed stack (`docker compose --profile all down -v`, no `.env`). Measured
span across six cold runs by two people: **43–61 s** — compose up 23–31 s,
Iceberg load 14–26 s, views ~2 s, smoke test ~1 s.

The load is the variable part, and the variance is all in the *first* table:
`adventure_works_dw_build_version` is a single row and took anywhere from 5.6 s
to 11.4 s, while the remaining 28 tables together take ~15 s. That is namespace
creation plus the MinIO first-object 403 retry backoff (see the gotchas), not
anything about data volume — no speed conclusion should be drawn from it.

Two caveats on the numbers: those machines already had the Docker images and the
`uv` cache, so a genuinely first-ever run adds the image pull (~1 GB for this
profile) and the dependency download. And step 1, the extract, is a one-time cost
on top — **33 s** measured under DW-8 with the SQL Server image and the `.bak`
already cached, or roughly 10 min on a machine that has to fetch both.

### Checking the lake is still correct

`smoke_test.py` asks whether the engines agree. It does **not** compare the lake
against the source files, so it would not notice a loader regression — which is
what DW-18, DW-19 and DW-20 all were, and all three were invisible to a green
`load_iceberg.py` run.

```bash
uv run python scripts/verify_lake.py            # ~17 s, exits non-zero on a bad lake
uv run python scripts/verify_lake.py --list     # the eleven checks
uv run python scripts/verify_lake.py --check null_reconciliation
uv run python scripts/verify_lake.py --strict   # an unreachable engine fails too
```

It reads the source once and the lake once, then runs eleven checks over those
two summaries: row counts, type fidelity, nullability flags *and* that the
constraint still rejects a NULL, null-count reconciliation, the DW-18/DW-19 value
invariants, **a value-for-value digest of all 343 columns**, referential
integrity including the three role-playing date keys, decimal exactness, and
cross-engine agreement.

The value digest earns its place: without it, counts and types and flags all pass
while an entire column's contents are wrong. Independent review replaced a whole
string column, zeroed an int column, and shifted every date in `dim_date` by one
day — the conformed dimension every fact joins to — and every other check stayed
green.

**Expectations are derived from the source on every run, not hardcoded**, so the
checks do not rot as the data changes — the one exception is `known_baseline`,
which asserts 1,060,715 rows / 29 tables / 343 columns precisely, so that drift
in the *source* is caught too.

Engines that are down are **named in the output**, never silently skipped, and an
unreachable lake fails with an actionable message rather than a screen of
reassuring skips. The run ends by listing what it does *not* check.

### Beyond ClickHouse

The sequence above is the ClickHouse path. For the other engines:

```bash
docker compose --profile engines up -d   # + Trino + DuckDB HTTP
uv run python scripts/sync_postgres.py   # Iceberg -> Postgres copy (Tier 2)
uv run python scripts/smoke_test.py      # compare every engine
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

**Then connect with a ClickHouse driver over HTTP, not a Postgres driver.**
Measured under DW-13; the reasoning is below the recipe because the obvious
choice is the wrong one.

| | |
| --- | --- |
| Driver | ClickHouse (DBeaver's **ClickHouse (Legacy)** works; so does the modern one) |
| Host / port | `localhost` : `8123` |
| Database | `raw` |
| User / password | `default` / `clickhouse` |
| Credentials | in **exactly one place** — see the Code 516 trap below |

**Why not the Postgres driver on 9005, which looks easier?** It runs queries
fine and a navigator still stays empty. Two independent reasons, and the second
is the durable one:

1. **`pg_catalog` is not a database here.** Qualified `pg_catalog.pg_class`
   answers *"Database pg_catalog does not exist"*. Unqualified `pg_class`,
   `pg_namespace`, `pg_attribute` and `pg_type` *do* resolve over 9005, but
   they are content-free stubs — `pg_class` has no `relname` column at all and
   `pg_namespace` does not list `raw` — so they cannot enumerate anything. Every
   real driver schema-qualifies, so it never reaches them anyway.
2. **ClickHouse's parser rejects the SQL these drivers emit**, whatever the
   catalog contains: bare `~`, `!~`, `OPERATOR(pg_catalog.~)` and `E'...'`
   escape-string literals are all syntax errors. So populating `pg_catalog`
   later would not fix this.

Verified by driving the real PostgreSQL JDBC driver (pgjdbc 42.7.4, what DBeaver
and DataGrip use) through `DatabaseMetaData`: `getSchemas`, `getTables`,
`getColumns` and `getCatalogs` **all fail**, while a plain query returns 18,484.

```bash
# queries: fine
PGPASSWORD=clickhouse psql -h 127.0.0.1 -p 9005 -U default -d default \
  -c 'SELECT count() FROM raw.dim_customer'          # -> 18484

# browsing: not fine
psql ... -c '\dt raw.*'
# ERROR: Unrecognized token ... at '~' in OPERATOR(pg_catalog.~)
#        server closed the connection unexpectedly
psql ... -c 'SELECT count() FROM pg_catalog.pg_class'
# DB::Exception: Database pg_catalog does not exist
```

**Two more things about 9005 if you use it as a query fallback.** A JDBC client
needs `?sslmode=disable`: ClickHouse answers the Postgres `SSLRequest` with `S`
("willing") and then drops the TLS handshake, so pgjdbc's default
`sslmode=prefer` fails at *connect* time with *"SSL error: Remote host
terminated the handshake"* rather than falling back. (`psql` is unaffected —
libpq negotiates differently, which is why the recipe above works.) And **any
error kills the session**, so a typo means reconnecting.

Over HTTP, by contrast, every metadata surface a tool needs answers:
`system.tables` and `system.columns` return 29 and 343 for `raw`, as does
`information_schema`, and `DESCRIBE TABLE raw.dim_customer` returns 29 columns.

So: **HTTP 8123 to browse, pgwire 9005 as a query fallback.** `engines.yaml`
carries both ports for that reason.

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
- **Types are pinned, never inferred.** `shared/scripts/extract_source.py` writes
  authoritative column types from SQL Server's `INFORMATION_SCHEMA` to
  `shared/schemas/*.json`, and the loader uses those. CSV type inference disagrees
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

Both are enforced in [`scripts/_common.py`](scripts/_common.py). The extract
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
  (checked `system.settings` and `system.server_settings`). Confirmed still live
  on 26.7.3.19: sending both headers returns 516, while either alone succeeds.

  The fix is **credentials in exactly one place**, not a different transport —
  either `Authorization: Basic` alone (DBeaver's **ClickHouse (Legacy)** driver)
  or `X-ClickHouse-User/Key` alone (the modern driver). Both work; the failure
  is only ever the *pair*, typically caused by filling in the auth fields *and*
  putting credentials in the JDBC URL.

  A **Postgres driver on port 9005** sidesteps the trap, and is a reasonable
  query fallback — but do not reach for it to browse: pgwire has no
  `pg_catalog`, so the navigator stays empty (see *Making ClickHouse browsable
  in a GUI* above).
  ```bash
  PGPASSWORD=clickhouse psql -h 127.0.0.1 -p 9005 -U default -d default \
    -c 'SELECT count() FROM raw.dim_customer'
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
- **Catalog attachment cannot half-succeed, but a failed attach is not
  surfaced by `up`.** Measured under DW-11. `CREATE DATABASE … DataLakeCatalog`
  validates eagerly against Lakekeeper, so a broken attach leaves **no**
  database rather than an empty or half-working one — a wrong URI fails with
  `Code: 198 … DNS_ERROR` and a wrong warehouse with `Code: 86 … 404 NoSuchWarehouseException`,
  and in both cases `system.databases` has no `datalake` row. `attach-catalog.sh`
  runs `set -euo pipefail`, so `clickhouse-init` exits non-zero.

  The gap is that **`docker compose up -d` returns 0 even when a one-shot
  service fails** (verified). So the symptom is visible but not announced: you
  get `Code: 81 … Database datalake does not exist` at first query.

  **`--wait` does not fix this, despite looking like it should.** It does exit 1
  when a one-shot fails — but it also exits 1 when every one-shot *succeeds*,
  printing `container clickhouse-init exited (0)`, because Compose counts any
  exited container as a failure regardless of exit code. It therefore cannot
  distinguish the two cases and must not go in the quickstart, where it would
  make every good run look broken. (An earlier version of this note recommended
  it on the strength of the failure case alone; corrected under DW-14.) Check
  directly instead:
  ```bash
  docker logs clickhouse-init          # want: "catalog attached as database 'datalake'"
  docker exec clickhouse clickhouse-client --query 'SHOW TABLES FROM datalake' | wc -l   # want 29
  ```
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

Shown from the repo root, because this stack reads the shared raw layer and
nothing here works without it.

```
data-warehouse-local/
├── pyproject.toml                    # one venv for the whole repo — which is why
├── uv.lock                           #   `uv sync` works from this directory
├── CLAUDE.md  DECISIONS.md  README.md
│
├── shared/                           # the raw layer, shared by every stack
│   ├── data/adventure_works_dw/      # extracted CSVs (gitignored, ~182 MB)
│   ├── schemas/                      # pinned column types (checked in, 29 files)
│   └── scripts/
│       ├── extract_source.py         # .bak -> SQL Server -> bcp -> CSV + schemas
│       └── common.py                 # paths, snake_case, exclusions
│
├── stacks/
│   ├── iceberg-multi-engine/         # ← you are here
│   │   ├── engines.yaml              # contract consumed by the BI app
│   │   ├── docker-compose.yml
│   │   ├── .env.example              # copy to .env; credentials and ports
│   │   ├── scripts/
│   │   │   ├── load_iceberg.py       # CSV -> Iceberg `raw`  (canonical loader)
│   │   │   ├── create_clickhouse_views.py  # Iceberg -> browsable `raw` views
│   │   │   ├── sync_postgres.py      # Iceberg -> Postgres   (full refresh)
│   │   │   ├── smoke_test.py         # query every engine, compare results
│   │   │   ├── _common.py            # config, snake_case, retry helper
│   │   │   └── _manifest.py          # engines.yaml reader
│   │   └── services/
│   │       ├── clickhouse/{config.d,users.d,attach-catalog.sh}
│   │       ├── duckdb-api/           # FastAPI service
│   │       ├── lakekeeper/           # bootstrap + warehouse template
│   │       ├── postgres/init-databases.sql
│   │       └── trino/catalog/iceberg.properties
│   └── clickhouse/                   # off-pipeline CSV -> MergeTree; see DW-15
│
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
