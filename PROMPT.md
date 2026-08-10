# Project brief

## What this is

The data backend for a **custom BI application** — a from-scratch alternative to
Tableau and Power BI — which is being built in a separate project.

This repo owns the data layer and the engine endpoints. It does not contain any
application code.

## What it is for

**1. Give the BI app several real engines to talk to.**

The app needs to connect to genuinely different SQL engines so its dialect
layer, type handling, and metadata discovery get exercised for real: Trino,
DuckDB (both over HTTP and embedded in-process), ClickHouse, and Postgres.
Where the data came from is irrelevant to this goal — the app just sees SQL
endpoints.

**2. Test whether Iceberg can be a neutral storage layer.**

The architectural question: can one lake serve every engine, rather than
maintaining one ETL pipeline per engine?

The answer so far is *partly*, and the partial answer is the useful result.
Trino, DuckDB, and ClickHouse read the same Iceberg tables in place, zero copy.
Postgres cannot — no mature native Iceberg reader exists — so it takes a synced
copy. The shape that emerges is one ingest pipeline plus a small number of thin,
uniform sync jobs, rather than N bespoke pipelines from source. That is a real
improvement even though it is not the pure zero-copy ideal.

## Dataset

AdventureWorks DW 2022, chosen because it is a realistic star schema with
recognisable business entities and a proper date dimension, and it reloads in
seconds.

It is **too small to say anything about performance** (~1M rows total, largest
star-schema fact table ~60k). Every engine answers instantly. No speed
conclusions should be drawn from this repo.

## Decisions made

| Decision | Rationale |
| --- | --- |
| Iceberg as the table format | Broadest engine support; the flexibility being bought |
| Lakekeeper as the catalog | Reasonable open REST catalog, takes access control seriously; no reason to switch |
| One canonical namespace (`raw`) | The app needs one obvious place to point |
| snake_case identifiers | Removes an entire class of cross-engine case-folding bug |
| Pinned schemas, never inferred | Otherwise each engine gets a subtly different view of the same data |
| Full-refresh Postgres sync | Seconds to reload at this size; incremental would add drift bugs for no benefit |
| ClickHouse over StarRocks | Far lighter, and StarRocks' advantage (MVs over Iceberg) is invisible at this volume |
| Druid dropped | Cannot read Iceberg — it is an ETL target, not an engine, and cost 6 containers |

## Deliberately out of scope

- **Benchmarking.** An earlier version of this repo timed three loaders against
  each other. That measured CSV parsing, not Iceberg, on a dataset far too small
  to differentiate engines. The apparatus is parked under
  `experiments/loader-comparison/`.
- **Performance tuning.** No partitioning, no materialised views, no caching.
  All premature at this volume.
- **Snowflake.** Wants real cloud object storage; deferred until there is a
  bucket, or until connector testing alone justifies a trial account.

## Possible next directions

- Add a `marts` namespace with pre-aggregated tables once raw star-schema
  queries prove awkward to chart directly. This is the likely first real need,
  and it is where the serving-layer question starts to matter.
- Add Snowflake once cloud storage exists, to test the neutral-layer thesis
  against a commercial engine.
- Revisit StarRocks if the dataset grows enough for materialised views to show
  a difference.
- Incremental sync from Iceberg snapshots, if the full refresh ever gets slow
  enough to be annoying.
