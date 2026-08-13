"""Expose the Iceberg lake as browsable ClickHouse views.

DataLakeCatalog resolves tables lazily and never registers them in
`system.tables` / `system.columns`. `SHOW TABLES FROM datalake` lists them, but
anything that introspects the system tables — DBeaver, most BI tools, ClickHouse's
own information_schema — sees an empty database.

Wrapping each Iceberg table in a plain view fixes that: views *are* registered,
so tools can browse them with real column types and autocomplete.

It also drops an awkward quoting rule. ClickHouse folds the Iceberg namespace
into the table name, so the direct reference is `datalake."raw.dim_customer"` —
a dot inside a quoted identifier, which GUIs routinely mangle. The views live in
a `raw` database, making the reference `raw.dim_customer`, identical in shape to
the Postgres copy.

The views are pass-through, so there is no second copy of the data.

Usage:
    uv run python scripts/create_clickhouse_views.py
    uv run python scripts/create_clickhouse_views.py --drop
"""

from __future__ import annotations

import argparse
import os

import clickhouse_connect
from rich.console import Console

from _common import NAMESPACE

console = Console()

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse")

CATALOG_DB = "datalake"
VIEW_DB = NAMESPACE  # `raw`, mirroring the Iceberg namespace and Postgres schema


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--drop", action="store_true", help=f"Drop the `{VIEW_DB}` database and exit.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASSWORD
    )
    try:
        if args.drop:
            client.command(f"DROP DATABASE IF EXISTS {VIEW_DB}")
            console.print(f"[yellow]dropped[/yellow] database {VIEW_DB}")
            return

        # SHOW TABLES is the only way to enumerate a DataLakeCatalog database —
        # system.tables is empty for it, which is the whole reason we are here.
        tables = [r[0] for r in client.query(f"SHOW TABLES FROM {CATALOG_DB}").result_rows]
        tables = [t for t in tables if t.startswith(f"{NAMESPACE}.")]
        if not tables:
            console.print(
                f"[red]No tables in {CATALOG_DB} under namespace '{NAMESPACE}'.[/red] "
                "Run scripts/load_iceberg.py first."
            )
            raise SystemExit(1)

        client.command(f"CREATE DATABASE IF NOT EXISTS {VIEW_DB}")
        console.print(f"[cyan]Creating[/cyan] {len(tables)} views in {VIEW_DB}")

        for i, qualified in enumerate(sorted(tables), 1):
            short = qualified[len(NAMESPACE) + 1:]
            client.command(
                f'CREATE OR REPLACE VIEW {VIEW_DB}.`{short}` AS '
                f'SELECT * FROM {CATALOG_DB}."{qualified}"'
            )
            console.print(f"  ({i}/{len(tables)}) {VIEW_DB}.{short}")

        registered = client.query(
            f"SELECT count() FROM system.columns WHERE database = '{VIEW_DB}'"
        ).result_rows[0][0]
        console.print(
            f"\n[bold green]Done.[/bold green] {len(tables)} views, "
            f"{registered:,} columns now visible to introspection"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
