"""Load Adventure Works DW CSVs into Iceberg via Apache DataFusion.

Target: namespace `datafusion` under warehouse `adventure_works_dw`.

DataFusion (the Python binding) owns CSV parsing and schema inference, producing
Arrow tables. PyIceberg is used as the write shim because iceberg-rust does not
yet expose a stable Python write path against a REST catalog. Timing is captured
across the full read + write so it is comparable to the other loaders.
"""

from __future__ import annotations

import time
from pathlib import Path

import datafusion
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError
from rich.console import Console

from _common import CATALOG_NAME, CATALOG_URI, WAREHOUSE, list_csv_files, s3_properties
from _results import TableResult, total_rows, total_seconds

console = Console()
LOADER = "datafusion"
NAMESPACE = (LOADER,)


def get_catalog() -> RestCatalog:
    return RestCatalog(
        CATALOG_NAME,
        **{
            "uri": CATALOG_URI,
            "warehouse": WAREHOUSE,
            **s3_properties(),
        },
    )


def reset_namespace(catalog: RestCatalog) -> None:
    try:
        for ident in catalog.list_tables(NAMESPACE):
            catalog.drop_table(ident)
        catalog.drop_namespace(NAMESPACE)
    except NoSuchNamespaceError:
        pass
    catalog.create_namespace(NAMESPACE)


def load_one(
    ctx: datafusion.SessionContext,
    catalog: RestCatalog,
    csv_path: Path,
) -> TableResult:
    start = time.perf_counter()
    df = ctx.read_csv(str(csv_path), has_header=True)
    arrow_tbl = df.to_arrow_table()

    ident = (*NAMESPACE, csv_path.stem)
    try:
        catalog.drop_table(ident)
    except NoSuchTableError:
        pass
    iceberg_tbl = catalog.create_table(ident, schema=arrow_tbl.schema)
    iceberg_tbl.append(arrow_tbl)
    return TableResult(
        loader=LOADER,
        table=csv_path.stem,
        rows=arrow_tbl.num_rows,
        seconds=time.perf_counter() - start,
    )


def run(quiet: bool = False) -> list[TableResult]:
    ctx = datafusion.SessionContext()
    catalog = get_catalog()
    if not quiet:
        console.print(f"[cyan]Resetting namespace[/cyan] {NAMESPACE[0]}")
    reset_namespace(catalog)

    csv_files = list_csv_files()
    if not quiet:
        console.print(f"[cyan]Loading[/cyan] {len(csv_files)} CSV files via DataFusion")
    results: list[TableResult] = []
    for i, csv_path in enumerate(csv_files, 1):
        r = load_one(ctx, catalog, csv_path)
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
