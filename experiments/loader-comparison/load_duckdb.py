"""Load Adventure Works DW CSVs into Iceberg via DuckDB.

Target: namespace `duckdb` under warehouse `adventure_works_dw`.

Requires DuckDB >= 1.2 with the `iceberg` and `httpfs` extensions and write
support against a REST catalog (Lakekeeper). For each CSV we use
read_csv_auto + CREATE TABLE AS SELECT against the attached catalog.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import duckdb
from rich.console import Console

from _common import (
    CATALOG_URI,
    S3_ACCESS_KEY,
    S3_ENDPOINT,
    S3_REGION,
    S3_SECRET_KEY,
    WAREHOUSE,
    list_csv_files,
)
from _results import TableResult, total_rows, total_seconds

console = Console()
LOADER = "duckdb"
SCHEMA = LOADER
CATALOG_ALIAS = "ice"


def s3_endpoint_parts() -> tuple[str, str]:
    """Return (host:port, use_ssl_bool_str) — DuckDB S3 secret wants host:port."""
    parsed = urlparse(S3_ENDPOINT)
    host = parsed.netloc or parsed.path
    use_ssl = "true" if parsed.scheme == "https" else "false"
    return host, use_ssl


def configure(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSTALL iceberg")
    con.execute("LOAD iceberg")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")

    endpoint_host, use_ssl = s3_endpoint_parts()
    con.execute(
        f"""
        CREATE OR REPLACE SECRET minio_secret (
            TYPE s3,
            KEY_ID '{S3_ACCESS_KEY}',
            SECRET '{S3_SECRET_KEY}',
            ENDPOINT '{endpoint_host}',
            REGION '{S3_REGION}',
            URL_STYLE 'path',
            USE_SSL {use_ssl}
        )
        """
    )
    con.execute(
        f"""
        ATTACH '{WAREHOUSE}' AS {CATALOG_ALIAS} (
            TYPE iceberg,
            ENDPOINT '{CATALOG_URI}'
        )
        """
    )


def reset_schema(con: duckdb.DuckDBPyConnection) -> None:
    existing = con.execute(
        f"SELECT table_name FROM duckdb_tables() "
        f"WHERE database_name = '{CATALOG_ALIAS}' AND schema_name = '{SCHEMA}'"
    ).fetchall()
    for (tname,) in existing:
        con.execute(f'DROP TABLE IF EXISTS {CATALOG_ALIAS}.{SCHEMA}."{tname}"')
    con.execute(f"DROP SCHEMA IF EXISTS {CATALOG_ALIAS}.{SCHEMA} CASCADE")
    con.execute(f"CREATE SCHEMA {CATALOG_ALIAS}.{SCHEMA}")


def load_one(con: duckdb.DuckDBPyConnection, csv_path: Path) -> TableResult:
    start = time.perf_counter()
    table_name = csv_path.stem
    con.execute(
        f'CREATE TABLE {CATALOG_ALIAS}.{SCHEMA}."{table_name}" AS '
        f"SELECT * FROM read_csv_auto(?, header=true)",
        [str(csv_path)],
    )
    rows = con.execute(
        f'SELECT COUNT(*) FROM {CATALOG_ALIAS}.{SCHEMA}."{table_name}"'
    ).fetchone()[0]
    return TableResult(
        loader=LOADER,
        table=table_name,
        rows=rows,
        seconds=time.perf_counter() - start,
    )


def run(quiet: bool = False) -> list[TableResult]:
    con = duckdb.connect()
    configure(con)
    if not quiet:
        console.print(f"[cyan]Resetting schema[/cyan] {CATALOG_ALIAS}.{SCHEMA}")
    reset_schema(con)

    csv_files = list_csv_files()
    if not quiet:
        console.print(f"[cyan]Loading[/cyan] {len(csv_files)} CSV files via DuckDB")
    results: list[TableResult] = []
    for i, csv_path in enumerate(csv_files, 1):
        r = load_one(con, csv_path)
        results.append(r)
        if not quiet:
            console.print(
                f"  ({i}/{len(csv_files)}) {r.table}: "
                f"[green]{r.rows:,}[/green] rows in [yellow]{r.seconds:.2f}s[/yellow]"
            )
    return results


def main() -> None:
    results = run()
    console.print(
        f"[bold green]Done.[/bold green] {total_rows(results):,} rows across "
        f"{len(results)} tables in {total_seconds(results):.2f}s"
    )


if __name__ == "__main__":
    main()
