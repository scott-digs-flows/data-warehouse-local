"""Load the shared raw layer into ClickHouse MergeTree tables.

Reads shared/data/*.csv plus the pinned types in shared/schemas/*.json, and
writes native ClickHouse tables. No object store, no catalog, no Iceberg — this
stack is self-contained.

Types come from the pinned schemas rather than CSV inference, so this stack and
any other stack built on the same raw layer agree on what the data is.

Usage:
    docker compose up -d
    uv run python load.py
    uv run python load.py --table DimCustomer --table DimDate
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import clickhouse_connect
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from dotenv import load_dotenv
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "scripts"))
import common  # noqa: E402

console = Console()
STACK_DIR = Path(__file__).resolve().parent
load_dotenv(STACK_DIR / ".env", override=False)

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse")
DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "raw")


# SQL Server type -> (Arrow type for reading, ClickHouse type for the DDL).
#
# Kept as one table so the two can never drift: the Arrow type decides how the
# CSV is parsed, the ClickHouse type decides how it is stored, and a mismatch
# between them shows up as an insert error rather than silently wrong data.
def type_pair(col: dict) -> tuple[pa.DataType, str]:
    t = col["sql_type"]
    p, s = col.get("precision") or 38, col.get("scale") or 0
    match t:
        case "bit":
            return pa.bool_(), "Bool"
        case "tinyint":
            return pa.int32(), "UInt8"
        case "smallint":
            return pa.int32(), "Int16"
        case "int":
            return pa.int32(), "Int32"
        case "bigint":
            return pa.int64(), "Int64"
        case "real":
            return pa.float32(), "Float32"
        case "float":
            return pa.float64(), "Float64"
        case "decimal" | "numeric":
            return pa.decimal128(p, s), f"Decimal({p}, {s})"
        case "money":
            return pa.decimal128(19, 4), "Decimal(19, 4)"
        case "smallmoney":
            return pa.decimal128(10, 4), "Decimal(10, 4)"
        case "date":
            return pa.date32(), "Date32"
        case "datetime" | "datetime2" | "smalldatetime":
            return pa.timestamp("us"), "DateTime64(6)"
        case "datetimeoffset":
            return pa.timestamp("us", tz="UTC"), "DateTime64(6, 'UTC')"
        case _:
            # Includes `time`, which ClickHouse has no native type for.
            return pa.string(), "String"


def strip_nul_bytes(tbl: pa.Table) -> pa.Table:
    """Turn embedded NUL bytes in text columns into proper nulls.

    `bcp -k` writes some NULL nvarchar values as a literal 0x00 byte rather than
    an empty field (287 rows each in DimProduct's Spanish and French names).
    Cleaning here keeps every stack's view of the data identical.
    """
    for i, field in enumerate(tbl.schema):
        if not pa.types.is_string(field.type):
            continue
        col = tbl.column(i)
        if not pc.any(pc.match_substring(col, "\x00")).as_py():
            continue
        cleaned = pc.replace_substring(col, pattern="\x00", replacement="")
        cleaned = pc.if_else(pc.equal(cleaned, ""), pa.scalar(None, pa.string()), cleaned)
        tbl = tbl.set_column(i, field, cleaned)
    return tbl


def read_csv(csv_path: Path, cols: list[dict]) -> pa.Table:
    tbl = pacsv.read_csv(
        csv_path,
        convert_options=pacsv.ConvertOptions(
            column_types={c["source_name"]: type_pair(c)[0] for c in cols},
            include_columns=[c["source_name"] for c in cols],
            # bcp writes NULL as an empty field, for text columns too.
            strings_can_be_null=True,
        ),
    )
    return strip_nul_bytes(tbl.rename_columns([c["name"] for c in cols]))


def order_by(cols: list[dict]) -> str:
    """Pick a sorting key.

    ORDER BY is ClickHouse's main performance lever, so prefer the table's
    surrogate key when there is a non-nullable one. `tuple()` (no sorting) is
    the honest fallback — better than inventing a key that misleads later
    benchmarking.
    """
    for c in cols:
        if c["name"].endswith("_key") and not c["nullable"]:
            return c["name"]
    return "tuple()"


def ddl(table: str, cols: list[dict]) -> str:
    defs = []
    for c in cols:
        ch_type = type_pair(c)[1]
        if c["nullable"]:
            ch_type = f"Nullable({ch_type})"
        defs.append(f'    `{c["name"]}` {ch_type}')
    return (
        f"CREATE TABLE {DATABASE}.`{table}` (\n"
        + ",\n".join(defs)
        + f"\n) ENGINE = MergeTree ORDER BY {order_by(cols)}"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", action="append", dest="tables",
                   help="Source table name (e.g. DimCustomer). Repeatable. Default: all.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    csv_files = common.list_csv_files()
    if args.tables:
        wanted = set(args.tables)
        csv_files = [p for p in csv_files if p.stem in wanted]
        missing = wanted - {p.stem for p in csv_files}
        if missing:
            console.print(f"[red]No CSV for:[/red] {', '.join(sorted(missing))}")
    if not csv_files:
        console.print(
            "[red]Nothing to load.[/red] Run shared/scripts/extract_source.py first."
        )
        raise SystemExit(1)

    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASSWORD
    )
    try:
        client.command(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
        console.print(f"[cyan]Loading[/cyan] {len(csv_files)} tables into {DATABASE}")

        total = 0
        failures: list[tuple[str, str]] = []
        for i, csv_path in enumerate(csv_files, 1):
            start = time.perf_counter()
            try:
                spec = common.load_schema(csv_path)
                cols = common.wanted_columns(spec)
                table = spec["table"]
                arrow_tbl = read_csv(csv_path, cols)

                client.command(f"DROP TABLE IF EXISTS {DATABASE}.`{table}`")
                client.command(ddl(table, cols))
                client.insert_arrow(f"{DATABASE}.{table}", arrow_tbl)

                total += arrow_tbl.num_rows
                console.print(
                    f"  ({i}/{len(csv_files)}) {table}: "
                    f"[green]{arrow_tbl.num_rows:,}[/green] rows in "
                    f"[yellow]{time.perf_counter() - start:.2f}s[/yellow]"
                )
            except Exception as e:
                failures.append((csv_path.stem, f"{type(e).__name__}: {e}"))
                console.print(f"  ({i}/{len(csv_files)}) {csv_path.stem}: [red]FAILED[/red]")

        console.print(
            f"\n[bold green]Loaded[/bold green] {total:,} rows across "
            f"{len(csv_files) - len(failures)} tables"
        )
        if failures:
            console.print(f"\n[bold red]{len(failures)} failed:[/bold red]")
            for name, err in failures:
                console.print(f"  [red]•[/red] {name}: {err}")
            raise SystemExit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
