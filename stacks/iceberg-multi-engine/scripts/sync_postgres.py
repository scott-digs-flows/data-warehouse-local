"""Sync the Iceberg lake into Postgres — the one Tier-2 engine.

Postgres has no mature native Iceberg reader, so unlike Trino / DuckDB /
ClickHouse it cannot query the lake in place and needs a copy. This is the
concrete cost of the copy-vs-zero-copy tradeoff: one extra job to run, one more
thing that can go stale.

Strategy: full refresh. The whole dataset is under 200 MB and reloads in
seconds, so an incremental path would add snapshot bookkeeping and a class of
drift bugs for no benefit at this size. Iceberg snapshots make incremental
possible later without changing anything upstream.

Usage:
    uv run python scripts/sync_postgres.py
    uv run python scripts/sync_postgres.py --table dim_customer
"""

from __future__ import annotations

import argparse
import os
import time

import psycopg
import pyarrow as pa
from pyiceberg.catalog.rest import RestCatalog
from rich.console import Console

from _common import (
    CATALOG_NAME,
    CATALOG_URI,
    NAMESPACE,
    WAREHOUSE,
    s3_properties,
    with_s3_retry,
)

console = Console()

PG_DSN = os.environ.get(
    "POSTGRES_ANALYTICS_DSN",
    "postgresql://{user}:{pw}@localhost:{port}/{db}".format(
        user=os.environ.get("POSTGRES_USER", "lakekeeper"),
        pw=os.environ.get("POSTGRES_PASSWORD", "lakekeeper"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        db=os.environ.get("POSTGRES_ANALYTICS_DB", "analytics"),
    ),
)
TARGET_SCHEMA = NAMESPACE
BATCH_ROWS = 50_000


def pg_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_int32(t):
        return "integer"
    if pa.types.is_int64(t):
        return "bigint"
    if pa.types.is_float32(t):
        return "real"
    if pa.types.is_float64(t):
        return "double precision"
    if pa.types.is_decimal(t):
        return f"numeric({t.precision},{t.scale})"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    if pa.types.is_time(t):
        return "time"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "bytea"
    return "text"


def get_catalog() -> RestCatalog:
    return RestCatalog(
        CATALOG_NAME,
        **{"uri": CATALOG_URI, "warehouse": WAREHOUSE, **s3_properties()},
    )


def sync_table(conn: psycopg.Connection, catalog: RestCatalog, table_name: str) -> tuple[int, float]:
    start = time.perf_counter()
    arrow_tbl = with_s3_retry(
        lambda: catalog.load_table((NAMESPACE, table_name)).scan().to_arrow(),
        f"read {table_name}",
    )

    cols = [(f.name, pg_type(f.type)) for f in arrow_tbl.schema]
    ddl_cols = ", ".join(f'"{n}" {t}' for n, t in cols)
    col_list = ", ".join(f'"{n}"' for n, _ in cols)
    fq = f'"{TARGET_SCHEMA}"."{table_name}"'

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {fq}")
        cur.execute(f"CREATE TABLE {fq} ({ddl_cols})")
        with cur.copy(f"COPY {fq} ({col_list}) FROM STDIN") as copy:
            for batch in arrow_tbl.to_batches(max_chunksize=BATCH_ROWS):
                for row in zip(*[c.to_pylist() for c in batch.columns]):
                    copy.write_row(row)
    conn.commit()
    return arrow_tbl.num_rows, time.perf_counter() - start


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", action="append", dest="tables",
                   help="Iceberg table name (snake_case). Repeatable. Default: all.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    catalog = get_catalog()
    tables = [ident[-1] for ident in catalog.list_tables((NAMESPACE,))]
    if args.tables:
        wanted = set(args.tables)
        missing = wanted - set(tables)
        if missing:
            console.print(f"[red]Not in the lake:[/red] {', '.join(sorted(missing))}")
        tables = [t for t in tables if t in wanted]
    if not tables:
        console.print("[red]Nothing to sync.[/red] Run scripts/load_iceberg.py first.")
        raise SystemExit(1)

    console.print(f"[cyan]Full refresh[/cyan] of {len(tables)} tables into Postgres {TARGET_SCHEMA}")
    total = 0
    failures: list[tuple[str, str]] = []
    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{TARGET_SCHEMA}"')
        conn.commit()

        for i, name in enumerate(sorted(tables), 1):
            try:
                rows, secs = sync_table(conn, catalog, name)
                total += rows
                console.print(
                    f"  ({i}/{len(tables)}) {name}: "
                    f"[green]{rows:,}[/green] rows in [yellow]{secs:.2f}s[/yellow]"
                )
            except Exception as e:
                conn.rollback()
                failures.append((name, f"{type(e).__name__}: {e}"))
                console.print(f"  ({i}/{len(tables)}) {name}: [red]FAILED[/red]")

    console.print(f"\n[bold green]Synced[/bold green] {total:,} rows into Postgres")
    if failures:
        console.print(f"\n[bold red]{len(failures)} failed:[/bold red]")
        for name, err in failures:
            console.print(f"  [red]•[/red] {name}: {err}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
