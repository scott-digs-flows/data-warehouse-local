"""Query every engine in engines.yaml and compare the answers.

This is the reference implementation of what the BI app does: read the manifest,
connect with each engine's driver, build an engine-specific table reference from
the `qualify` template, run the same logical query, normalise the results.

If this passes, the app has a working contract to build against. If an engine
disagrees on a row count, something is wrong with the lake — not with the app.

Usage:
    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --engine trino --engine postgres
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import requests
from rich.console import Console
from rich.table import Table as RichTable

import _manifest
from _common import (
    CATALOG_URI,
    S3_ACCESS_KEY,
    S3_ENDPOINT,
    S3_REGION,
    S3_SECRET_KEY,
    WAREHOUSE,
)

console = Console()

PROBE_TABLE = "fact_internet_sales"

# Deliberately plain SQL: no dialect-specific functions, so any divergence in
# the results is about the data or the engine's type handling, not about syntax.
COUNT_SQL = "SELECT COUNT(*) FROM {ref}"
AGG_SQL = (
    "SELECT sales_territory_key, COUNT(*) AS orders, SUM(sales_amount) AS total "
    "FROM {ref} GROUP BY sales_territory_key ORDER BY sales_territory_key"
)


# --------------------------------------------------------------------------
# One executor per driver. Each returns a list of row tuples.
# --------------------------------------------------------------------------
def run_trino(engine: _manifest.Engine, sql: str) -> list[tuple]:
    from trino.dbapi import connect

    c = engine.connection
    conn = connect(host=c["host"], port=c["port"], user=c["user"],
                   catalog=c["catalog"], schema=c["schema"])
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()
    finally:
        conn.close()


def run_http(engine: _manifest.Engine, sql: str) -> list[tuple]:
    r = requests.post(engine.dsn, json={"sql": sql, "limit": 10_000}, timeout=60)
    r.raise_for_status()
    return [tuple(row) for row in r.json()["rows"]]


def run_duckdb_embedded(engine: _manifest.Engine, sql: str) -> list[tuple]:
    import duckdb

    parsed = urlparse(S3_ENDPOINT)
    attach = engine.connection["attach_sql"].format(
        s3_access_key=S3_ACCESS_KEY,
        s3_secret_key=S3_SECRET_KEY,
        s3_endpoint_host=engine.connection.get("s3_endpoint_host")
        or parsed.netloc or parsed.path,
        s3_region=S3_REGION,
        warehouse=WAREHOUSE,
        catalog_uri=CATALOG_URI,
    )
    con = duckdb.connect()
    try:
        for stmt in filter(None, (s.strip() for s in attach.split(";"))):
            con.execute(stmt)
        return con.execute(sql).fetchall()
    finally:
        con.close()


def run_clickhouse(engine: _manifest.Engine, sql: str) -> list[tuple]:
    import clickhouse_connect

    c = engine.connection
    client = clickhouse_connect.get_client(
        host=c["host"], port=c["http_port"],
        username=c["user"], password=c.get("password", ""),
    )
    try:
        return [tuple(r) for r in client.query(sql).result_rows]
    finally:
        client.close()


def run_postgres(engine: _manifest.Engine, sql: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(engine.dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


EXECUTORS = {
    "trino": run_trino,
    "http": run_http,
    "duckdb": run_duckdb_embedded,
    "clickhouse-connect": run_clickhouse,
    "psycopg": run_postgres,
}


def normalise(v: Any) -> Any:
    """Make values comparable across engines.

    Engines return the same number as int, Decimal, float — or, over the DuckDB
    HTTP endpoint, as a *string*, because JSON has no decimal type and the
    service stringifies anything non-primitive. That transport-level type loss
    is worth knowing about: a BI app consuming JSON over HTTP has to re-infer
    numeric types that a native driver would have preserved.
    """
    if isinstance(v, (Decimal, float)):
        return round(float(v), 2)
    if isinstance(v, str):
        try:
            return round(float(Decimal(v)), 2)
        except (ArithmeticError, ValueError):
            return v
    return v


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--engine", action="append", dest="engines",
                   help="Limit to specific engine(s). Repeatable. Default: all.")
    p.add_argument("--table", default=PROBE_TABLE, help=f"Probe table (default: {PROBE_TABLE}).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    manifest = _manifest.load()
    engines = manifest.engines
    if args.engines:
        wanted = set(args.engines)
        engines = [e for e in engines if e.name in wanted]
        unknown = wanted - set(manifest.names)
        if unknown:
            console.print(f"[red]Unknown engine(s):[/red] {', '.join(sorted(unknown))}")

    console.print(
        f"Probing [bold]{args.table}[/bold] across {len(engines)} engines "
        f"({manifest.dataset['warehouse']}.{manifest.dataset['namespace']})\n"
    )

    results = RichTable(show_header=True, header_style="bold")
    results.add_column("Engine")
    results.add_column("Tier")
    results.add_column("Dialect")
    results.add_column("Table reference")
    results.add_column("Rows", justify="right")
    results.add_column("Groups", justify="right")
    results.add_column("Status")

    counts: dict[str, int] = {}
    aggs: dict[str, list] = {}
    failures: list[tuple[str, str]] = []

    for engine in engines:
        ref = engine.table_ref(args.table)
        executor = EXECUTORS.get(engine.driver)
        if executor is None:
            failures.append((engine.name, f"no executor for driver {engine.driver!r}"))
            results.add_row(engine.name, engine.tier, engine.dialect, ref, "-", "-", "[red]no driver[/red]")
            continue
        try:
            n = executor(engine, COUNT_SQL.format(ref=ref))[0][0]
            agg = [tuple(normalise(v) for v in row)
                   for row in executor(engine, AGG_SQL.format(ref=ref))]
            counts[engine.name] = int(n)
            aggs[engine.name] = agg
            results.add_row(engine.name, engine.tier, engine.dialect, ref,
                            f"{int(n):,}", str(len(agg)), "[green]ok[/green]")
        except Exception as e:
            failures.append((engine.name, f"{type(e).__name__}: {e}"))
            results.add_row(engine.name, engine.tier, engine.dialect, ref, "-", "-", "[red]FAILED[/red]")

    console.print(results)

    # The point of the exercise: every engine must see identical data.
    console.print()
    if len(set(counts.values())) == 1 and counts:
        console.print(f"[bold green]✓ All engines agree[/bold green] on {next(iter(counts.values())):,} rows")
    elif counts:
        console.print("[bold red]✗ Row counts disagree:[/bold red]")
        for name, n in counts.items():
            console.print(f"    {name}: {n:,}")

    distinct_aggs = {name: agg for name, agg in aggs.items()}
    if distinct_aggs:
        baseline_name, baseline = next(iter(distinct_aggs.items()))
        mismatched = [n for n, a in distinct_aggs.items() if a != baseline]
        if mismatched:
            console.print(
                f"[bold red]✗ Aggregate results differ[/bold red] from {baseline_name}: "
                f"{', '.join(mismatched)}"
            )
        else:
            console.print(
                f"[bold green]✓ All engines agree[/bold green] on the aggregate "
                f"({len(baseline)} territory groups, sums matched to 2dp)"
            )

    if failures:
        console.print(f"\n[bold red]{len(failures)} engine(s) failed:[/bold red]")
        for name, err in failures:
            console.print(f"  [red]•[/red] {name}: {err}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
