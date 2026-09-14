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
| Raw layer = CSV + pinned schemas | Engine-neutral, zero services. *(Amended 2026-09-11: this row used to end "and the thing that actually makes new stacks cheap. Not Iceberg." That framing belonged to the superseded position. The raw layer is still CSV plus pinned schemas — it is the **input to** Iceberg, not an alternative to it, and what makes engines cheap is now the lake.)* |
| ~~Stacks are self-contained~~ *(superseded 2026-09-11)* | Run one engine at a time without dragging up a lake, a catalog, and a metastore. — Superseded with the row above: the lake and catalog are now the point, not overhead to avoid. Engines sit behind compose profiles instead, so you still pay only for the engine you want. |
| ~~Iceberg is one strategy, not the foundation~~ *(superseded 2026-09-11)* | Its payoff is zero-copy across *concurrently running* engines. Running one engine at a time, a typed extract does the same job for free. — **Sound for the goal it served**, which was comparing self-contained per-engine stacks. Superseded when engine-neutrality became the project's purpose rather than one of several experiments: see the row below. |
| **Iceberg is the mandatory middle layer** *(2026-09-11)* | Every engine reads from Iceberg; no engine owns the data. Rejected the alternative this supersedes — a typed CSV extract as the neutral layer — because it makes neutrality a property each new loader has to re-implement correctly, rather than a property of the storage. The payoff is not performance, which this dataset cannot show: it is that adding an engine costs a compose service and an `engines.yaml` entry instead of a new ETL pipeline. The cost is real and worth stating — a catalog service and an object store must be running before anything can be read, where a CSV needs nothing. Revisit if that operational cost ever outweighs having one copy of the data, e.g. for a consumer that genuinely cannot reach object storage. |
| Types pinned from `INFORMATION_SCHEMA`, never inferred | CSV inference disagrees between readers (int32 vs int64, string vs date, decimal handling). Inference would give each stack a subtly different view of the same data, making any cross-stack difference impossible to attribute. |
| snake_case identifiers | Engines disagree on case folding — Trino lowercases, Postgres folds unquoted names, ClickHouse is case-sensitive, Snowflake uppercases. Normalising once at load time removes the whole class of bug. |
| ~~ClickHouse as the default stack~~ *(superseded 2026-09-11)* | One container, no catalog, none of the Iceberg failure modes. — This described `stacks/clickhouse/`, the off-pipeline CSV→MergeTree path, and is exactly the reasoning that keeps a shortcut attractive. ClickHouse is now the first **engine**, reading Iceberg in place through `stacks/iceberg-multi-engine/`; it owns no data. The off-pipeline stack was removed under DW-15; see the row below. |
| ClickHouse over StarRocks | Far lighter, and StarRocks' real advantage (materialised views over Iceberg) is invisible at this data volume. |
| Druid dropped | It cannot read Iceberg — `druid-iceberg-extensions` is an *ingestion input source* that copies into Druid's own segment format. That makes it an ETL target, not an engine, and it cost six containers. |
| Lakekeeper as the Iceberg catalog | Reasonable open REST catalog that takes access control seriously. No reason found to switch. |
| The off-pipeline `stacks/clickhouse/` was removed, not converted *(2026-09-11)* | It loaded CSV straight into MergeTree, bypassing Iceberg. Rejected converting it into an Iceberg reader: `stacks/iceberg-multi-engine/ --profile clickhouse` already *is* ClickHouse reading Iceberg, so "conversion" meant deleting its loader, pointing its compose at MinIO and Lakekeeper — destroying the self-containment that was its only distinguishing property — and arriving at a duplicate. The deciding evidence was not architectural though: by removal it **disagreed with the lake about the data**, still carrying the pre-DW-18 `NA`-to-NULL bug and the pre-DW-19 NCHAR(0) handling, and its DDL declared columns non-nullable while `insert_arrow` silently coerced NULL to `''` (see DW-21). A second answer to "what is the data" is exactly what the pinned-schema invariant exists to prevent. Its useful residue was already preserved elsewhere or actively wrong — its DBeaver advice recommended the pgwire route DW-13 disproved. Recoverable from git history if ever needed. |
| Lake verification is a separate tool from the smoke test *(2026-09-11)* | `verify_lake.py` reconciles the lake against `shared/data` and `shared/schemas`; `smoke_test.py` asks whether the engines agree and a client can reach them. Rejected folding the checks into `smoke_test.py`, which is the tempting option since both "verify things": that script is the reference implementation of what the BI app does, and **the app has no access to the source CSVs or the pinned schemas**. Teaching it to read them would make it a worse example of the one thing it exists to demonstrate. The cost of two tools is real — they overlap on cross-engine agreement — and is paid down by `verify_lake.py` importing `smoke_test`'s executors rather than reimplementing them, so there is one implementation of "how to query engine X". |
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
- **An engine can accept a NULL into a column it says is non-nullable, and say
  nothing.** ClickHouse 26.7.3.19, via `clickhouse_connect.insert_arrow`:

  ```
  CREATE TABLE probe.t (`k` Int32, `color` String) ENGINE = MergeTree ORDER BY k
  insert_arrow  pa.array(["Red", None, "Blue"])
    -> SUCCEEDED
       (1, 'Red',  is_empty=0)
       (2, '',     is_empty=1)   <- the NULL, silently coerced
       (3, 'Blue', is_empty=0)
  ```

  The column is `String`, not `Nullable(String)`. Nothing rejected the NULL; it
  became an empty string and the insert reported success.

  Found in the off-pipeline CSV→MergeTree loader removed under DW-15, where it
  composed with a second silent failure: `dim_product.color` is pinned NOT NULL
  and holds the literal source value `NA` in 254 rows, those were parsed to NULL
  by the `null_values` default (DW-18), and the insert then coerced them to `''`
  — producing a value present in neither the source nor the lake, with no error
  anywhere in the run.

  **Why this is written here rather than tracked as work.** There is nothing to
  fix: the loader that hit it is deleted, and the architecture now forbids
  loading source files into an engine's native storage at all. But the hazard
  belongs to the *combination* — a MergeTree DDL generated from pinned schemas,
  plus an Arrow insert — not to that loader, so the next engine on that path
  will meet it. The lesson generalises past ClickHouse: **"the insert succeeded"
  is not evidence the data is right.** Validate nullability before the write,
  the way `apply_pinned_nullability()` does on the Iceberg path, where a NULL in
  a pinned-NOT-NULL column raises `ValueError: Casting field 'x' with null
  values to non-nullable` and fails the load loudly (DW-20). Then read back and
  reconcile against the source.

- **Naming convention got 39 of 44 foreign keys right — and the 5 it missed are
  the interesting ones.** The `*_key` convention was trusted to derive the star
  schema's joins. Extracting `sys.foreign_keys` turned that assertion into a
  measurement (DW-25), and the two sets differ in *both* directions despite both
  numbering 44:

  * 4 declared FKs are the `parent_*_key` self-references, which the derivation
    routes to a hierarchy check instead — a different check, not a gap.
  * 1 is a **composite**: `fact_internet_sales_reason` joins
    `fact_internet_sales` on `(sales_order_number, sales_order_line_number)`.
    No single-column naming rule can express it, and nothing checked it before.
  * 5 edges the convention *invents* — plausible relationships like
    `fact_survey_response.product_category_key` that AdventureWorks never
    declared as constraints. The data satisfies them, so they are checked
    anyway, but they are not contractual.

  The lesson generalises past this dataset: **convention is a good default and a
  bad oracle.** Derivation covers the common case cheaply and fails loudly on a
  key it cannot place; extraction is what tells you whether the derivation is
  right. Keeping both, and diffing them, is worth more than either alone.

- **A fabricated constraint that agrees with the data is invisible to every
  check that validates against data.** This is the failure mode an *authority
  artifact* is uniquely exposed to, and it is worth understanding before adding a
  second source of truth of any kind.

  Three bogus foreign-key edges were planted into the verifier's curated list.
  Two were caught immediately — not by the minimality safeguard, which let all
  three through, but downstream, because the data contradicted them: a
  reversed-direction edge produced 18,148 duplicate target keys and 319 orphans,
  and a flattened composite produced 32,739 duplicates. The third was a duplicate
  of a real declared composite with its column pairs consistently reordered — the
  same relationship, differently spelled. Nothing objected, because there was
  nothing to object to: the relationship is real, so orphan and uniqueness tests
  pass. The suite checked it twice and reported it as a distinct, verified edge.

  The lesson is not "canonicalise your tuples". It is that **data-driven checks
  can only catch assertions the data disagrees with.** A claim that is true but
  redundant, or true but attributed to the wrong authority, is indistinguishable
  from a correct one by any amount of querying. The only defence is to check the
  *claim* against the *other sources of the claim* — which is why the curated
  list asserts its own minimality against extraction and derivation on every run,
  rather than being trusted because it is short.

  Corollary for anyone adding a third source later: the question to ask is not
  "is this edge correct?" but "is this edge *unreachable* by what we already
  have?" Only the second is falsifiable.

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

- ~~**Tune ClickHouse `ORDER BY`.**~~ *(obsolete 2026-09-11)* Described the
  MergeTree loader in `stacks/clickhouse/`, removed under DW-15. ClickHouse now
  reads Iceberg through pass-through views, which have no sorting key, so there
  is nothing here to tune. Kept visible because "`ORDER BY` is ClickHouse's main
  performance lever" is still true of any future MergeTree table.
- ~~**Add DuckDB or Trino stacks.**~~ *(obsolete 2026-09-11)* "A compose file
  plus a ~150-line `load.py` reading the same `shared/` artifacts" was the
  one-stack-per-engine model, superseded above. DuckDB and Trino are already
  engines in the single stack, behind compose profiles, reading the same Iceberg
  tables with no loader of their own — which is the point of the architecture.
- **Revisit StarRocks** if the dataset ever grows enough for materialised views
  over Iceberg to show a difference.
- **Incremental sync** from Iceberg snapshots, if a full refresh ever gets slow
  enough to be annoying.
