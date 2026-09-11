# Decisions and open directions

The README covers *what* this repo is and *how* to run it. This file covers
**why** it is shaped the way it is — the reasoning that is not recoverable from
the code, and the paths deliberately not taken.

## Purpose

Two independent goals, worth keeping separate because they have different
answers:

1. **Give the BI app real engines to talk to.** The app (a separate project)
   needs genuinely different SQL engines so its dialect layer, type handling,
   and metadata discovery get exercised. For this goal, where the data came from
   is irrelevant — the app just sees SQL endpoints.
2. **Test whether Iceberg can be a neutral storage layer.** Can one lake serve
   every engine instead of one pipeline per engine?

Conflating these is what made an earlier version of the repo confusing.

## Decisions

| Decision | Rationale |
| --- | --- |
| Raw layer = CSV + pinned schemas | Engine-neutral, zero services, and the thing that actually makes new stacks cheap. Not Iceberg. |
| Stacks are self-contained | Run one engine at a time without dragging up a lake, a catalog, and a metastore. |
| Iceberg is one strategy, not the foundation | Its payoff is zero-copy across *concurrently running* engines. Running one engine at a time, a typed extract does the same job for free. |
| Types pinned from `INFORMATION_SCHEMA`, never inferred | CSV inference disagrees between readers (int32 vs int64, string vs date, decimal handling). Inference would give each stack a subtly different view of the same data, making any cross-stack difference impossible to attribute. |
| snake_case identifiers | Engines disagree on case folding — Trino lowercases, Postgres folds unquoted names, ClickHouse is case-sensitive, Snowflake uppercases. Normalising once at load time removes the whole class of bug. |
| ClickHouse as the default stack | One container, no catalog, none of the Iceberg failure modes. |
| ClickHouse over StarRocks | Far lighter, and StarRocks' real advantage (materialised views over Iceberg) is invisible at this data volume. |
| Druid dropped | It cannot read Iceberg — `druid-iceberg-extensions` is an *ingestion input source* that copies into Druid's own segment format. That makes it an ETL target, not an engine, and it cost six containers. |
| Lakekeeper as the Iceberg catalog | Reasonable open REST catalog that takes access control seriously. No reason found to switch. |
| Full-refresh Postgres sync | Reloads in seconds at this size. Incremental would add snapshot bookkeeping and drift bugs for no benefit. |
| NULL and the empty string are not distinguished in the raw extract *(2026-09-11)* | `bcp` character format writes an unquoted empty field for both, so the distinction is destroyed before any loader runs — 191,778 values across 32 nullable columns are formally ambiguous. Rejected re-extracting with a NULL sentinel (a `bcp queryout` with per-column `ISNULL` mapping): it adds per-column SQL generation to the extract for a distinction that is semantically inert in a BI star schema, where "no value" is one concept. Accepted deliberately rather than engineered around. Revisit if a source arrives where empty string and NULL genuinely differ in meaning. |
| The lake enforces the source's `NOT NULL` constraints *(2026-09-11)* | The pinned schemas carry a `nullable` flag from `INFORMATION_SCHEMA`; until now the loader read it and never used it, so all 343 Iceberg fields were `optional` and the pinned schema was documentation rather than a constraint. 144 fields are now `required`. Rejected leaving it descriptive: a constraint the storage layer does not enforce is one a future load can break silently, and this lake's whole purpose is that every engine sees the same data. **Only became possible after DW-18 and DW-19** — pinned-NOT-NULL columns actually holding NULLs went 5 → 2 → 0 as those two defects were fixed. Attempted earlier it would have failed on real data, and relaxing the constraint would have looked like the sensible fix, ratifying the corruption. Revisit if a source arrives whose declared constraints its own data violates; the answer there is to fix the extract or correct the pinned schema, not to widen the lake. |
| Literal `NCHAR(0)` becomes the empty string at the Iceberg load boundary *(2026-09-11)* | `DimProduct`'s Spanish and French name columns hold 287 values each that are a single 0x00 byte, meaning "no translation available". They are pinned `NOT NULL`, so they were never NULLs. Rejected preserving the raw NUL (byte-faithful, but Postgres `text` rejects 0x00, pushing the workaround into one consumer) and rejected NULL (the previous behaviour — it asserts "unknown" where the source says "empty", and breaks the pinned NOT NULL contract). Empty string is safe in every engine and keeps the source's meaning. |

## Findings worth remembering

- **ClickHouse can read Iceberg with no catalog at all** — the `iceberg()` table
  function against a raw S3 path works. The minimum viable Iceberg layer is
  therefore object storage alone, not object storage plus a catalog service.
- **ClickHouse *can* write to Iceberg**, behind `allow_insert_into_iceberg=1`.
  The commits are real and Trino reads them — but PyIceberg cannot
  (`missing field-id`). Treat as read-only unless every reader tolerates it.
- **The dataset is too small for performance conclusions.** ~1M rows total,
  largest star-schema fact table ~60k. Every engine answers instantly.
- **A wire protocol that runs queries is not therefore browsable.**
  ClickHouse's Postgres wire emulation on 9005 executes SQL correctly, so it
  looks like the obvious route for a GUI — and this repo recommended it as
  such. It is not: there is no `pg_catalog`, which is exactly what Postgres
  drivers read to enumerate tables and columns, so a navigator stays empty
  while every query succeeds. `pg_catalog.pg_class` answers *"Database
  pg_catalog does not exist"*, and psql's `\dt` drops the connection on
  `OPERATOR(pg_catalog.~)`. Browse over HTTP 8123 with a ClickHouse driver,
  where `system.tables`, `system.columns` and `information_schema` all answer.
  Generalises beyond ClickHouse: when adding an engine, test *introspection*
  separately from *querying* — the BI app needs both, and they fail
  independently (DW-13).

- **Pinning types does not pin which *values* mean NULL.** A second, separate
  door for inference, and the more dangerous one because it is invisible in a
  green load. `pyarrow.csv` defaults `null_values` to a 17-token list — `NA`,
  `N/A`, `null`, `NaN`, `#N/A` and 12 others — and silently nulls any value
  matching one. AdventureWorks uses `NA` as a real value, so this destroyed 564
  source values across 5 columns until it was found by comparing the lake
  against the source rather than by reading the loader (DW-18). Any loader for
  any new source must state `null_values` explicitly. The general rule: the
  pinned-schema contract covers types, and *every* remaining inference the
  reader performs has to be pinned down separately and deliberately.
- **Full-refresh loading buys schema freedom, not just simplicity.** Iceberg
  lets you relax a field from `required` to `optional` but not the reverse,
  since existing rows may already hold nulls — so tightening nullability on a
  live table needs a real evolution path, or is simply refused. Because the
  loader drops and recreates every table on every run, nullability is set at
  creation and never evolved, and the question does not arise. Worth knowing
  before anyone "optimises" the loader into an incremental one: that would
  trade this away.
- **A loader that never reads back cannot detect its own corruption.**
  `load_iceberg.py` reports row counts from the in-memory Arrow table, so both
  the `NA` corruption and a total failure of the read path were invisible to a
  green run. Verification has to query the lake, not trust the loader's log.

## Deliberately out of scope

- **Benchmarking.** An earlier version timed three loaders against each other.
  It measured CSV parsing rather than Iceberg, on a dataset far too small to
  differentiate engines, and two of the three "loaders" performed an identical
  Iceberg write. Parked under `experiments/loader-comparison/` with a README
  explaining what a real writer comparison would need.
- **Performance tuning.** No partitioning, no materialised views, no caching.
  Premature at this volume.
- **Snowflake.** Supports Iceberg, but requires real cloud object storage and
  cannot read local MinIO. Deferred until there is a bucket — or just
  `COPY INTO` a trial account, since for connector testing the data's origin
  does not matter.

## Open directions

### A `marts` namespace

Reserved but empty. The likely first real need: raw star-schema queries get
awkward to chart directly, and dashboards want pre-aggregated tables. This is
also where the serving-layer question starts to matter — querying raw facts
directly is rarely fast enough for interactive dashboards at scale, which is
exactly what Snowflake and Databricks hide behind result caching.

### A semantic layer (Cube, or similar)

Investigated, not built. What already exists and is directly usable:

- `shared/schemas/*.json` is the physical layer — types, nullability, and the
  `source_name → snake_case` mapping.
- **29 fact→dimension join edges are derivable** from the `*_key` naming
  convention across 10 facts and 16 dimensions.

What naming convention *cannot* resolve, and would need extracting from SQL
Server's `sys.foreign_keys` (the same trick already used for types):

| Pattern | Example | What it is |
| --- | --- | --- |
| `*_alternate_key` | `dim_product.product_alternate_key` | Natural/business key — **not** a join |
| `parent_*_key` | `dim_employee.parent_employee_key` | Self-referencing hierarchy. 4 dimensions have these |
| `order_/due_/ship_date_key` | `fact_internet_sales` × 3 | **Role-playing dimensions** — one fact joins `dim_date` three times |
| `fact_*.<name>_key` | `fact_finance.finance_key` | Degenerate key — the fact's own surrogate PK |

The role-playing date dimensions matter most: every dashboard will care whether
it is filtering on order, due, or ship date, and naming inference cannot tell
them apart.

**Architectural caveat before adopting Cube:** it sits *between* the app and the
engines. That solves the SQL-dialect problem outright and brings caching and
pre-aggregations — but Cube is oriented around one data source, which partly
undercuts the goal of exercising the app against several engines. If the
multi-engine comparison still matters, keep the semantic model engine-neutral
(your own `model.json`) and treat Cube as one possible consumer.

### Smaller items

- **Tune ClickHouse `ORDER BY`.** The loader picks the first non-nullable
  `*_key` column, so `fact_internet_sales` sorts by `product_key`. That is a
  defensible default, not a tuned sorting key — and `ORDER BY` is ClickHouse's
  main performance lever, so revisit it before any performance work.
- **Add DuckDB or Trino stacks.** Each is a compose file plus a ~150-line
  `load.py` reading the same `shared/` artifacts.
- **Revisit StarRocks** if the dataset ever grows enough for materialised views
  over Iceberg to show a difference.
- **Incremental sync** from Iceberg snapshots, if a full refresh ever gets slow
  enough to be annoying.
