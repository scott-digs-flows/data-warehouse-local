# data-warehouse-local

A local sandbox for experimenting with CSV → Iceberg conversion across three
loader libraries (PyIceberg, DuckDB, Apache DataFusion), then querying the same
Iceberg tables from three engines (Trino, DuckDB-via-HTTP, Druid) — all running
in Docker against a Lakekeeper REST catalog and MinIO object storage.

Source dataset: Microsoft's **Adventure Works DW 2022** sample.

The downstream Dash BI app lives in a separate project; this repo only owns the
data layer and the engine endpoints.

## Architecture

```
                   ┌──────────────────────────┐
                   │       MinIO (S3)         │   warehouse bucket
                   └────────────┬─────────────┘
                                │ s3a://warehouse/adventure_works_dw/...
                   ┌────────────┴─────────────┐
                   │  Lakekeeper REST catalog │   warehouse=adventure_works_dw
                   └────────────┬─────────────┘
                                │  http://lakekeeper:8181/catalog
        ┌───────────────────────┼────────────────────────┐
        │                       │                        │
 ┌──────┴──────┐         ┌──────┴──────┐         ┌──────┴──────┐
 │  PyIceberg  │         │   DuckDB    │         │ DataFusion  │   loaders
 │  load_*     │         │  load_*     │         │  load_*     │   write CSV->
 └─────────────┘         └─────────────┘         └─────────────┘   namespace
                                                                  pyiceberg
                                                                  duckdb
                                                                  datafusion
        ┌───────────────────────┬────────────────────────┐
        │                       │                        │
 ┌──────┴──────┐         ┌──────┴──────┐         ┌──────┴──────┐
 │    Trino    │         │ DuckDB API  │         │   Druid     │   engines
 │   :8080     │         │   :8000     │         │   :8888     │
 └─────────────┘         └─────────────┘         └─────────────┘
        │                       │                        │
        └───────────────────────┴────────────────────────┘
                                ↓
                     (future) Python Dash app
```

| Component       | Role                              | Image / location                            |
| --------------- | --------------------------------- | ------------------------------------------- |
| MinIO           | S3-compatible object storage      | `minio/minio`                               |
| Postgres        | Catalog + Druid metadata          | `postgres:16`                               |
| Lakekeeper      | Iceberg REST catalog              | `quay.io/lakekeeper/catalog`                |
| Trino           | Query engine                      | `trinodb/trino`                             |
| DuckDB API      | FastAPI wrapper over DuckDB       | `./services/duckdb-api`                     |
| Druid           | OLAP engine                       | `apache/druid:31.0.1`                       |
| Zookeeper       | Coordinates Druid services        | `zookeeper:3.9`                             |

## Prerequisites

- Docker (with Compose v2)
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- ~6 GB free RAM for the full stack (Druid is the heaviest tenant)
- On Apple Silicon: SQL Server runs under amd64 emulation during the CSV
  extraction step

## Quickstart

```bash
# 1. Configuration
cp .env.example .env
# (edit if you want non-default ports/passwords)

# 2. Install Python deps
uv sync

# 3. Bring up the catalog + storage
docker compose up -d
# Wait until `docker compose ps` shows lakekeeper-bootstrap as Exited (0).

# 4. Download Adventure Works CSVs (~50 MB .bak, ~30 tables)
uv run python scripts/download_adventure_works.py

# 5. Run a single loader, or benchmark all three
uv run python scripts/load_pyiceberg.py
uv run python scripts/benchmark.py

# 6. Bring up the query engines
docker compose --profile engines up -d
# Trino UI:    http://localhost:8080
# DuckDB API:  http://localhost:8000/docs

# 7. Bring up Druid and ingest from Iceberg
docker compose --profile druid up -d
uv run python scripts/druid_ingest.py --source-namespace pyiceberg --wait
# Druid console: http://localhost:8888
```

## Directory layout

```
data-warehouse-local/
├── docker-compose.yml
├── .env.example
├── pyproject.toml
├── scripts/
│   ├── download_adventure_works.py   # bak -> SQL Server -> bcp -> CSV
│   ├── load_pyiceberg.py             # CSV -> namespace pyiceberg
│   ├── load_duckdb.py                # CSV -> namespace duckdb
│   ├── load_datafusion.py            # CSV -> namespace datafusion
│   ├── benchmark.py                  # runs all three, writes report
│   ├── druid_ingest.py               # submits Druid ingestion tasks
│   ├── _common.py                    # shared catalog + S3 config
│   └── _results.py                   # benchmark result dataclasses
├── services/
│   ├── duckdb-api/                   # FastAPI service
│   ├── trino/catalog/iceberg.properties
│   ├── lakekeeper/
│   │   ├── init-databases.sql        # Postgres DB bootstrap
│   │   └── create-warehouse.json     # warehouse creation payload
│   └── druid/environment             # Druid env config
├── data/                             # downloaded CSVs (gitignored)
└── reports/                          # benchmark output (gitignored)
```

## Database / namespace conventions

- Iceberg warehouse: `adventure_works_dw`
- Namespaces (one per loader): `pyiceberg`, `duckdb`, `datafusion`
- Table names: identical to source SQL Server table names (e.g. `DimCustomer`,
  `FactInternetSales`)

## Querying from each engine

### Trino

```bash
docker compose exec trino trino --catalog iceberg
trino:default> SHOW SCHEMAS;
trino:default> SELECT COUNT(*) FROM iceberg.pyiceberg."DimCustomer";
```

### DuckDB API

```bash
curl -X POST http://localhost:8000/query \
    -H 'Content-Type: application/json' \
    -d '{"sql": "SELECT COUNT(*) FROM ice.pyiceberg.\"DimCustomer\""}'
```

Browse interactive docs at <http://localhost:8000/docs>.

### Druid

After running `scripts/druid_ingest.py`, datasources are named
`<namespace>__<table>` (e.g. `pyiceberg__FactInternetSales`). Query via the
Druid console (<http://localhost:8888>) or the SQL API.

## Benchmarking

```bash
uv run python scripts/benchmark.py                          # all three
uv run python scripts/benchmark.py --only duckdb --repeat 3 # specific loader
```

Output: `reports/benchmark_<timestamp>.csv` and `.json` with per-table and
per-run timings.

## Troubleshooting

- **`lakekeeper-bootstrap` keeps restarting** — the warehouse may already
  exist. Re-run the curl manually or wipe the volume with
  `docker compose down -v`.
- **`SQL Server cannot find /var/opt/mssql/backup/...bak`** — the download
  step failed or the bak is truncated. Delete
  `data/adventure_works_dw/AdventureWorksDW2022.bak` and re-run.
- **DuckDB iceberg writes fail with "not supported"** — bump the DuckDB
  version in `pyproject.toml`. CTAS against attached REST catalogs landed
  in 1.2 and was made robust in 1.3.
- **Druid Iceberg ingestion task fails with classloader errors** — confirm
  `druid-iceberg-extensions` is in `druid_extensions_loadList` inside
  `services/druid/environment`.
- **Stack is slow on first start** — Druid takes ~60s to settle. Watch
  `docker compose logs -f druid-coordinator` until you see "Coordinator started".
