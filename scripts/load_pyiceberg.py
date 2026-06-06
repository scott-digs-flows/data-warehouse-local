"""Load Adventure Works DW CSVs into Iceberg via PyIceberg.

Target: namespace `pyiceberg` under warehouse `adventure_works_dw`.
"""

from __future__ import annotations

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchNamespaceError, NoSuchTableError
from rich.console import Console

from _common import CATALOG_NAME, CATALOG_URI, WAREHOUSE, list_csv_files, s3_properties
from _results import TableResult, total_rows, total_seconds

console = Console()
LOADER = "pyiceberg"
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


def read_csv_to_arrow(path: Path) -> pa.Table:
    # Default options autodetect types and treat empty fields as null.
    return pacsv.read_csv(path)


def load_one(catalog: RestCatalog, csv_path: Path) -> TableResult:
    start = time.perf_counter()
    arrow_tbl = read_csv_to_arrow(csv_path)
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
    catalog = get_catalog()
    if not quiet:
        console.print(f"[cyan]Resetting namespace[/cyan] {NAMESPACE[0]}")
    reset_namespace(catalog)

    csv_files = list_csv_files()
    if not quiet:
        console.print(f"[cyan]Loading[/cyan] {len(csv_files)} CSV files via PyIceberg")
    results: list[TableResult] = []
    for i, csv_path in enumerate(csv_files, 1):
        r = load_one(catalog, csv_path)
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
