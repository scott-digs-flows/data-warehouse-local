# data-warehouse-local

A local lakehouse that serves a **separate custom BI application**. This repo
owns the data layer and the engine endpoints — never app code.

## The canonical pipeline

```
raw files  ──►  Iceberg tables  ──►  warehouse engine
(CSV +          (the neutral        (ClickHouse today;
 pinned          storage layer,      others later)
 schemas)        engine-agnostic)
```

**Every engine reads from Iceberg.** That is the architectural commitment of
this project: engines are swappable because none of them owns the data. A path
that loads raw files straight into an engine's native storage is off-pipeline
and must not be introduced, however convenient it looks at this data volume.

The payoff is not performance — the dataset is far too small for that. It is
that adding an engine costs a compose service and a manifest entry, never a new
ETL pipeline.

### Current state vs. target

The repo predated this commitment. As of DW-15 it matches it — the off-pipeline
CSV → MergeTree stack has been removed, so there is no longer a path that reaches
an engine without going through Iceberg.

| Path | Status |
| --- | --- |
| `stacks/iceberg-multi-engine/` | **The pipeline.** The loader, catalog, and engine wiring live here. Now the only stack. |
| `experiments/loader-comparison/` | Parked. Do not extend. |
| `DECISIONS.md` | **Resolved (DW-16).** Iceberg is recorded as the mandatory middle layer; the old position is marked superseded and left visible rather than erased. |

`stacks/iceberg-multi-engine/` is the only stack, and the plural is now
historical. Engines live inside it behind compose profiles — that is the
extension point. **A new engine is a compose service plus an `engines.yaml`
entry; it is never a new stack, and never its own loader.** A second stack
would by construction be a second answer to what the data is, which is what
DW-15 removed.

## Invariants

These are load-bearing. Breaking one is a defect, not a style choice.

1. **Types are pinned, never inferred.** `shared/schemas/*.json` carries
   authoritative SQL Server types from `INFORMATION_SCHEMA`. CSV readers infer
   differently (int32 vs int64, string vs date, decimal handling); inference
   would give each engine a subtly different view of the same data, making any
   cross-engine discrepancy impossible to attribute.
2. **Identifiers are snake_case, normalised once at load.** `DimCustomer` →
   `dim_customer`. Engines disagree on case folding (Trino lowercases, Postgres
   folds unquoted, ClickHouse is case-sensitive, Snowflake uppercases).
3. **The raw extract stays faithful to the source.** Normalisation and column
   exclusions happen at the Iceberg load boundary, and are documented there.
4. **Loads are idempotent.** Re-running a loader is always safe.
5. **`engines.yaml` is the contract with the BI app.** Adding an engine means a
   compose service plus a manifest entry — never app code changes. Changing its
   shape is a breaking change for a consumer you cannot see from this repo.

## Layout

```
shared/
├── data/adventure_works_dw/   raw CSVs (gitignored, ~182 MB)
├── schemas/                   pinned column types (checked in, 29 tables)
└── scripts/
    ├── extract_source.py      .bak → SQL Server → bcp → CSV + schemas
    └── common.py              paths, snake_case, exclusions

stacks/iceberg-multi-engine/
├── engines.yaml               the BI app contract
├── docker-compose.yml         MinIO + Lakekeeper + Postgres, engines behind profiles
└── scripts/
    ├── load_iceberg.py        CSV → Iceberg `raw`   (the canonical loader)
    ├── create_clickhouse_views.py
    ├── smoke_test.py          cross-engine agreement check
    ├── _common.py             config, snake_case, S3 retry
    └── _manifest.py           engines.yaml reader
```

Warehouse `adventure_works_dw`, namespace `raw`. `marts` is reserved and empty.

## Dataset

AdventureWorks DW 2022: a real star schema, ~1.06M rows across 29 tables,
largest fact ~60k. **Sized for fast BI-app iteration, not for performance
measurement** — every engine answers instantly, so no speed conclusions can be
drawn here. Future sources (Tableau Super Store, Stack Overflow) land as
additional namespaces through the same pipeline.

## Working agreements

- **Jira project "Data Warehouse", key `DW`** on `scottdigsflows.atlassian.net`.
  All work is tracked there. See the `jira-workflow` skill.
- **Python is run through `uv`** (`uv run python ...`), never a bare interpreter.
- **Docker Compose commands run from inside the stack directory**, which owns
  its own `.env` and compose project name.
- **Gotchas are documented, not rediscovered.** The stack READMEs record failures
  that cost real debugging time. Read them before fighting an environment
  problem, and add to them when you lose time to something new.
- **Architectural changes get a `DECISIONS.md` entry.** See the
  `decision-record` skill.

## The team

Three agents, each with a distinct mandate — see `.claude/agents/`:

| Agent | Owns |
| --- | --- |
| `delivery-lead` | The Jira backlog: epics, stories, decomposition, status truth |
| `data-engineer` | Implementation: ingestion, engine wiring, the pipeline itself |
| `data-quality-analyst` | Verification: does the data in the engine match the source |

Procedures they share live in `.claude/skills/`.
