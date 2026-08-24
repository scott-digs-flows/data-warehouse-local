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

## Findings worth remembering

- **ClickHouse can read Iceberg with no catalog at all** — the `iceberg()` table
  function against a raw S3 path works. The minimum viable Iceberg layer is
  therefore object storage alone, not object storage plus a catalog service.
- **ClickHouse *can* write to Iceberg**, behind `allow_insert_into_iceberg=1`.
  The commits are real and Trino reads them — but PyIceberg cannot
  (`missing field-id`). Treat as read-only unless every reader tolerates it.
- **The dataset is too small for performance conclusions.** ~1M rows total,
  largest star-schema fact table ~60k. Every engine answers instantly.

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
