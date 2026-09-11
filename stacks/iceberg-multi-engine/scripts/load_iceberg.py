"""Load the AdventureWorks CSV extract into Iceberg. The one canonical loader.

Reads shared/schemas/<Table>.json (written by shared/scripts/extract_source.py)
rather than inferring types from CSV text, then writes to the `raw` namespace
with snake_case identifiers.

Why pinned schemas: every CSV reader infers types differently — int32 vs int64,
string vs date, decimal vs float. Left to inference, each engine would end up
with a subtly different view of the same data, and any cross-engine difference
the BI app hit would be impossible to attribute.

Usage:
    uv run python scripts/load_iceberg.py
    uv run python scripts/load_iceberg.py --table DimCustomer --table DimDate
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchNamespaceError, NoSuchTableError
from rich.console import Console

from _common import (
    CATALOG_NAME,
    CATALOG_URI,
    EXCLUDED_COLUMN_TYPES,
    NAMESPACE,
    SCHEMA_DIR,
    WAREHOUSE,
    list_csv_files,
    s3_properties,
    with_s3_retry,
)

console = Console()
NS = (NAMESPACE,)


# SQL Server type -> Arrow type.
#
# Iceberg has no 8- or 16-bit integer, so tinyint/smallint widen to int32.
# Binary columns never reach this function: wanted_columns() drops them via
# EXCLUDED_COLUMN_TYPES — a deliberate, documented narrowing of the source
# (the product/employee/territory photo blobs). See _common.py for why.
def arrow_type(col: dict) -> pa.DataType:
    t = col["sql_type"]
    if t == "bit":
        return pa.bool_()
    if t in ("tinyint", "smallint", "int"):
        return pa.int32()
    if t == "bigint":
        return pa.int64()
    if t == "real":
        return pa.float32()
    if t == "float":
        return pa.float64()
    if t in ("decimal", "numeric"):
        return pa.decimal128(col["precision"] or 38, col["scale"] or 0)
    if t == "money":
        return pa.decimal128(19, 4)
    if t == "smallmoney":
        return pa.decimal128(10, 4)
    if t == "date":
        return pa.date32()
    if t in ("datetime", "datetime2", "smalldatetime"):
        # Iceberg timestamps are microsecond precision; ns would be rejected.
        return pa.timestamp("us")
    if t == "datetimeoffset":
        return pa.timestamp("us", tz="UTC")
    if t == "time":
        return pa.time64("us")
    return pa.string()


def load_schema(csv_path: Path) -> dict:
    path = SCHEMA_DIR / f"{csv_path.stem}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no pinned schema at {path}. Re-run shared/scripts/extract_source.py."
        )
    return json.loads(path.read_text())


def wanted_columns(spec: dict) -> list[dict]:
    return [c for c in spec["columns"] if c["sql_type"] not in EXCLUDED_COLUMN_TYPES]


def strip_nul_bytes(tbl: pa.Table) -> pa.Table:
    """Turn embedded NUL bytes in text columns into proper nulls.

    287 rows each in DimProduct's Spanish and French name columns arrive as a
    single literal 0x00 byte. Parquet and Iceberg carry those bytes happily, but
    Postgres text columns reject NUL outright, so the sync fails downstream.
    Cleaning at the load boundary keeps every engine's view of the lake
    identical, instead of pushing the workaround into one consumer.

    Careful about *what those values are*. This once claimed they were NULLs that
    `bcp -k` had written as 0x00. That is wrong: both columns are pinned
    `nullable=false`, so SQL Server cannot have held a NULL there — they are
    literal NCHAR(0) values in the source, and the CSV contains zero empty
    strings for these columns. Converting them to NULL below is therefore a
    lossy choice, not a restoration of the source. Verified under DW-10; what
    they *should* become is DW-19.
    """
    for i, field in enumerate(tbl.schema):
        if not pa.types.is_string(field.type):
            continue
        col = tbl.column(i)
        if not pc.any(pc.match_substring(col, "\x00")).as_py():
            continue
        cleaned = pc.replace_substring(col, pattern="\x00", replacement="")
        # A value that was nothing but NUL bytes becomes NULL. Note this is a
        # lossy narrowing, not a round-trip: the source held NCHAR(0), not NULL.
        # Kept as-is pending DW-19 rather than changed mid-verification.
        cleaned = pc.if_else(pc.equal(cleaned, ""), pa.scalar(None, pa.string()), cleaned)
        tbl = tbl.set_column(i, field, cleaned)
    return tbl


def read_csv(csv_path: Path, spec: dict) -> pa.Table:
    """Parse CSV using the pinned types, then rename columns to snake_case."""
    cols = wanted_columns(spec)
    tbl = pacsv.read_csv(
        csv_path,
        convert_options=pacsv.ConvertOptions(
            column_types={c["source_name"]: arrow_type(c) for c in cols},
            include_columns=[c["source_name"] for c in cols],
            # bcp writes NULL as an empty field, for text columns too.
            strings_can_be_null=True,
            # An empty field is the ONLY thing that means NULL here. PyArrow
            # otherwise applies a 17-token default list -- "NA", "N/A", "null",
            # "NaN", "#N/A" and friends -- and silently nulls any value matching
            # one. AdventureWorks uses "NA" as a real value, which cost 564 rows
            # across 5 columns: dim_product.color (254) and .size_range (307),
            # and all three text columns of dim_sales_territory row 11, the star
            # schema's unknown member.
            #
            # Pinning types does not pin which *values* mean NULL, so this has to
            # be stated explicitly or inference creeps back in through the side
            # door. Keep it exhaustive rather than removing just the tokens that
            # bite today -- a future source containing "null" or "NaN" as real
            # text would otherwise hit the identical bug. See DW-18.
            null_values=[""],
        ),
    )
    tbl = tbl.rename_columns([c["name"] for c in cols])
    return strip_nul_bytes(tbl)


def get_catalog() -> RestCatalog:
    return RestCatalog(
        CATALOG_NAME,
        **{"uri": CATALOG_URI, "warehouse": WAREHOUSE, **s3_properties()},
    )


def ensure_namespace(catalog: RestCatalog) -> None:
    try:
        catalog.create_namespace(NS)
        console.print(f"[cyan]created namespace[/cyan] {NAMESPACE}")
    except NamespaceAlreadyExistsError:
        pass


def write_table(catalog: RestCatalog, ident: tuple[str, ...], arrow_tbl: pa.Table) -> None:
    """Create + populate one Iceberg table. Idempotent — safe to retry."""
    def once() -> None:
        try:
            catalog.drop_table(ident)
        except NoSuchTableError:
            pass
        # Unpartitioned by design: the largest table here is under a million
        # rows, so a partition spec would add complexity and change nothing.
        tbl = catalog.create_table(ident, schema=arrow_tbl.schema)
        tbl.append(arrow_tbl)

    with_s3_retry(once, ".".join(ident))


def load_one(catalog: RestCatalog, csv_path: Path) -> tuple[str, int, float]:
    start = time.perf_counter()
    spec = load_schema(csv_path)
    arrow_tbl = read_csv(csv_path, spec)
    write_table(catalog, (*NS, spec["table"]), arrow_tbl)
    return spec["table"], arrow_tbl.num_rows, time.perf_counter() - start


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", action="append", dest="tables",
                   help="Source table name (e.g. DimCustomer). Repeatable. Default: all.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    csv_files = list_csv_files()
    if args.tables:
        wanted = set(args.tables)
        csv_files = [p for p in csv_files if p.stem in wanted]
        missing = wanted - {p.stem for p in csv_files}
        if missing:
            console.print(f"[red]No CSV for:[/red] {', '.join(sorted(missing))}")
    if not csv_files:
        console.print("[red]Nothing to load.[/red] Run shared/scripts/extract_source.py first.")
        raise SystemExit(1)

    catalog = get_catalog()
    ensure_namespace(catalog)
    console.print(f"[cyan]Loading[/cyan] {len(csv_files)} tables into {WAREHOUSE}.{NAMESPACE}")

    total_rows = 0
    failures: list[tuple[str, str]] = []
    for i, csv_path in enumerate(csv_files, 1):
        try:
            name, rows, secs = load_one(catalog, csv_path)
            total_rows += rows
            console.print(
                f"  ({i}/{len(csv_files)}) {name}: "
                f"[green]{rows:,}[/green] rows in [yellow]{secs:.2f}s[/yellow]"
            )
        except Exception as e:  # keep going; report everything at the end
            failures.append((csv_path.stem, f"{type(e).__name__}: {e}"))
            console.print(f"  ({i}/{len(csv_files)}) {csv_path.stem}: [red]FAILED[/red]")

    console.print(
        f"\n[bold green]Loaded[/bold green] {total_rows:,} rows across "
        f"{len(csv_files) - len(failures)} tables"
    )
    if failures:
        console.print(f"\n[bold red]{len(failures)} failed:[/bold red]")
        for name, err in failures:
            console.print(f"  [red]•[/red] {name}: {err}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
