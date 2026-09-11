# data-warehouse-local

A local lakehouse backing a **custom BI application built in a separate
project**. One pipeline: raw files land in Iceberg, and every engine reads from
there. No engine owns the data, which is what makes adding one cost a compose
service and an `engines.yaml` entry rather than a new ETL pipeline.

```
shared/                       the raw layer — no services, always available
├── data/                     AdventureWorks DW CSVs (gitignored)
├── schemas/                  pinned column types (checked in)
└── scripts/
    ├── extract_source.py     .bak -> SQL Server -> bcp -> CSV + schemas
    └── common.py             paths, snake_case, exclusions

stacks/
└── iceberg-multi-engine/     Iceberg lake (MinIO + Lakekeeper), engines behind
                              compose profiles: ClickHouse, Trino, DuckDB, Postgres

experiments/
└── loader-comparison/        parked: PyIceberg vs DuckDB vs DataFusion writers
```

## The raw layer is the contract

`shared/data/*.csv` plus `shared/schemas/*.json` is the engine-neutral source of
truth. The schemas carry authoritative SQL Server types (precision, scale,
nullability) and the `PascalCase → snake_case` mapping.

Every loader reads types from there rather than inferring them from CSV text,
because inference disagrees between readers (int32 vs int64, string vs date,
decimal handling) and would give each engine a subtly different view of the same
bytes.

**The raw layer is the input to Iceberg, not an alternative to it.** This
section used to argue the opposite — that a typed CSV extract made Iceberg
optional, and that the default was therefore a plain single-engine stack. That
position is **superseded**; see `DECISIONS.md`, where it is kept visible along
with why it changed. Every engine now reads from Iceberg and no engine owns the
data, which is what makes adding one cost a compose service and an
`engines.yaml` entry rather than a new pipeline.

## Quickstart

```bash
uv sync

# One-time: extract source data (~10 min, spins up SQL Server under emulation)
uv run python shared/scripts/extract_source.py

# Then bring up the lake and an engine
cd stacks/iceberg-multi-engine
cp .env.example .env
docker compose --profile clickhouse up -d
uv run python scripts/load_iceberg.py            # CSV -> Iceberg `raw`
uv run python scripts/create_clickhouse_views.py # expose them to ClickHouse
uv run python scripts/smoke_test.py --engine clickhouse
```

That stack's [README](stacks/iceberg-multi-engine/README.md) carries the full
sequence, including the `/etc/hosts` prerequisite and the runtime it actually
takes. **Follow it rather than this summary** if anything here does not work.

## The stack

### [`iceberg-multi-engine/`](stacks/iceberg-multi-engine/)

MinIO + Lakekeeper hold the lake; engines sit behind compose profiles, so you
pay only for the one you want. ClickHouse, Trino and DuckDB read the *same*
Iceberg tables zero-copy; Postgres takes a synced copy because it has no mature
Iceberg reader. An `engines.yaml` manifest is the contract with the BI app, and
a cross-engine smoke test proves the connectors agree.

There used to be a second, self-contained `clickhouse/` stack that loaded CSV
straight into MergeTree. It was **removed (DW-15)**: it bypassed Iceberg
entirely, and by the end it also disagreed with the lake about the data —
carrying two load-boundary fixes the Iceberg path had received and silently
coercing NULLs into non-nullable columns. `DECISIONS.md` records why.

## Why it is shaped this way

[DECISIONS.md](DECISIONS.md) records the reasoning behind the structure, the
paths deliberately not taken, and open directions — including a worked-through
sketch of what a semantic layer would need.

## Prerequisites

- Docker with Compose v2
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- On Apple Silicon, SQL Server runs under amd64 emulation during extraction only

The stack additionally needs `127.0.0.1 lakekeeper` and `127.0.0.1 minio` in
`/etc/hosts` — see its README. Without them, host-side scripts fail in
confusing, unrelated-looking ways.

## Dataset

Microsoft's AdventureWorks DW 2022: a real star schema, ~1M rows, small enough
to reload in seconds. Sized for fast application iteration, **not** for
performance measurement — every engine answers instantly at this volume, so no
speed conclusions should be drawn from it.
