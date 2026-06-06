"""Benchmark CSV -> Iceberg conversion across PyIceberg, DuckDB, DataFusion.

Runs each loader fresh (drops + recreates its namespace), captures per-table and
total wall-clock + row counts, and writes a CSV + JSON report to reports/.

Usage:
    uv run python scripts/benchmark.py                  # all three loaders
    uv run python scripts/benchmark.py --only duckdb    # one of: pyiceberg, duckdb, datafusion
    uv run python scripts/benchmark.py --repeat 3       # average over N runs per loader
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from _common import REPORTS_DIR
from _results import TableResult, to_dicts, total_rows, total_seconds

import load_datafusion
import load_duckdb
import load_pyiceberg

console = Console()

LOADERS = {
    "pyiceberg": load_pyiceberg.run,
    "duckdb": load_duckdb.run,
    "datafusion": load_datafusion.run,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", choices=sorted(LOADERS), action="append",
                   help="Limit to specific loader(s). Repeatable.")
    p.add_argument("--repeat", type=int, default=1, help="Runs per loader (default 1).")
    return p.parse_args()


def run_loader(name: str, repeat: int) -> list[TableResult]:
    """Run a loader `repeat` times; return the last run's per-table results plus
    a synthetic '__total__' row for each run carrying its total wall-clock."""
    results: list[TableResult] = []
    last_per_table: list[TableResult] = []
    for run_idx in range(1, repeat + 1):
        console.rule(f"[bold cyan]{name}[/bold cyan] (run {run_idx}/{repeat})")
        t0 = time.perf_counter()
        per_table = LOADERS[name](quiet=False)
        wall = time.perf_counter() - t0
        results.extend(per_table)
        results.append(TableResult(loader=name, table="__total__", rows=total_rows(per_table),
                                   seconds=wall))
        last_per_table = per_table
        console.print(
            f"[bold]{name} run {run_idx}[/bold]: "
            f"sum-of-table={total_seconds(per_table):.2f}s  wall={wall:.2f}s  "
            f"rows={total_rows(per_table):,}"
        )
    return results


def print_summary(all_results: list[TableResult]) -> None:
    summary = Table(title="Benchmark Summary", show_header=True, header_style="bold")
    summary.add_column("Loader")
    summary.add_column("Runs", justify="right")
    summary.add_column("Tables", justify="right")
    summary.add_column("Rows", justify="right")
    summary.add_column("Avg wall (s)", justify="right")
    summary.add_column("Avg sum-of-tables (s)", justify="right")

    by_loader: dict[str, list[TableResult]] = {}
    for r in all_results:
        by_loader.setdefault(r.loader, []).append(r)

    for loader, rs in by_loader.items():
        totals = [r for r in rs if r.table == "__total__"]
        per_tables = [r for r in rs if r.table != "__total__"]
        runs = len(totals)
        tables_per_run = len(per_tables) // runs if runs else 0
        rows = totals[0].rows if totals else 0
        avg_wall = sum(t.seconds for t in totals) / runs if runs else 0
        avg_sum = sum(per_tables[i].seconds for i in range(len(per_tables))) / runs if runs else 0
        summary.add_row(
            loader, str(runs), str(tables_per_run), f"{rows:,}",
            f"{avg_wall:.2f}", f"{avg_sum:.2f}",
        )
    console.print(summary)


def write_reports(all_results: list[TableResult]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = REPORTS_DIR / f"benchmark_{stamp}.csv"
    json_path = REPORTS_DIR / f"benchmark_{stamp}.json"

    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["loader", "table", "rows", "seconds"])
        writer.writeheader()
        for d in to_dicts(all_results):
            writer.writerow(d)

    with open(json_path, "w") as fh:
        json.dump(
            {"generated_at": stamp, "results": to_dicts(all_results)},
            fh, indent=2,
        )
    return csv_path, json_path


def main() -> None:
    args = parse_args()
    loaders = args.only or list(LOADERS)

    all_results: list[TableResult] = []
    for loader_name in loaders:
        all_results.extend(run_loader(loader_name, args.repeat))

    console.rule("[bold]Summary")
    print_summary(all_results)
    csv_path, json_path = write_reports(all_results)
    console.print(f"[green]wrote[/green] {csv_path}")
    console.print(f"[green]wrote[/green] {json_path}")


if __name__ == "__main__":
    main()
