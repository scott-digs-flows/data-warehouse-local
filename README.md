# data-warehouse-local

A monorepo for experimenting with different local data-warehouse strategies,
backing a **custom BI application built in a separate project**.

Each strategy is a **self-contained stack**. Stacks share source data but never
each other's services — you run one at a time, and nothing is interconnected at
runtime.

```
shared/                       the raw layer — no services, always available
├── data/                     AdventureWorks DW CSVs (gitignored)
├── schemas/                  pinned column types (checked in)
└── scripts/
    ├── extract_source.py     .bak -> SQL Server -> bcp -> CSV + schemas
    └── common.py             paths, snake_case, exclusions

stacks/
├── clickhouse/               1 container · MergeTree · no catalog
└── iceberg-multi-engine/     6 containers · Iceberg lake · Trino + DuckDB + ClickHouse + Postgres

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

# Then pick a stack
cd stacks/clickhouse
cp .env.example .env
docker compose up -d
uv run python load.py
```

## Stacks

### [`clickhouse/`](stacks/clickhouse/) — the default

One container. CSV → MergeTree. 1,060,715 rows in 9.5 MiB, loads in seconds.
No object store, no catalog, none of the Iceberg failure modes. Connect a GUI
with a **Postgres driver on port 9005**.

### [`iceberg-multi-engine/`](stacks/iceberg-multi-engine/) — the shared-lake reference

Six containers: MinIO + Lakekeeper catalog, with Trino, DuckDB, and ClickHouse
reading the *same* Iceberg tables zero-copy, and Postgres taking a synced copy.
Has per-engine compose profiles, an `engines.yaml` manifest, and a cross-engine
smoke test proving all five connectors return identical results.

Kept because it is the working reference for the neutral-layer thesis, and the
place to go when zero-copy or time travel actually matters. Costs nothing while
stopped. Its README documents a long list of hard-won gotchas.

## Running two stacks at once

Supported but not the point. Each stack pins its own compose project name and
volumes, and ports are chosen not to collide (ClickHouse's native protocol sits
on 9010 because MinIO owns 9000). Usually you want just one up.

## Why it is shaped this way

[DECISIONS.md](DECISIONS.md) records the reasoning behind the structure, the
paths deliberately not taken, and open directions — including a worked-through
sketch of what a semantic layer would need.

## Prerequisites

- Docker with Compose v2
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- On Apple Silicon, SQL Server runs under amd64 emulation during extraction only

The Iceberg stack additionally needs `127.0.0.1 lakekeeper` and
`127.0.0.1 minio` in `/etc/hosts` — see its README. The ClickHouse stack needs
nothing beyond Docker.

## Dataset

Microsoft's AdventureWorks DW 2022: a real star schema, ~1M rows, small enough
to reload in seconds. Sized for fast application iteration, **not** for
performance measurement — every engine answers instantly at this volume, so no
speed conclusions should be drawn from it.
