"""Submit Druid ingestion tasks that read directly from Iceberg via Lakekeeper.

Uses the native druid-iceberg-extensions input source. Manual trigger — re-run
whenever you want Druid to refresh from the lake.

Examples:
    # Ingest all tables in the pyiceberg namespace
    uv run python scripts/druid_ingest.py --source-namespace pyiceberg

    # Ingest specific tables, with a real timestamp column
    uv run python scripts/druid_ingest.py --source-namespace pyiceberg \\
        --table FactInternetSales --time-column OrderDate

    # Wait for completion (poll Overlord)
    uv run python scripts/druid_ingest.py --source-namespace pyiceberg --wait
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests
from pyiceberg.catalog.rest import RestCatalog
from rich.console import Console

from _common import (
    CATALOG_NAME,
    CATALOG_URI,
    S3_ACCESS_KEY,
    S3_ENDPOINT,
    S3_REGION,
    S3_SECRET_KEY,
    WAREHOUSE,
    s3_properties,
)

console = Console()

DRUID_OVERLORD_URL = "http://localhost:8081"
DEFAULT_MISSING_TS = "2020-01-01T00:00:00Z"

# Lakekeeper endpoint as seen from *inside* the Druid containers (network alias).
INTRA_CATALOG_URI = "http://lakekeeper:8181/catalog"
INTRA_S3_ENDPOINT = "http://minio:9000"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-namespace", default="pyiceberg",
                   choices=["pyiceberg", "duckdb", "datafusion"],
                   help="Iceberg namespace to ingest from (default: pyiceberg).")
    p.add_argument("--table", action="append", dest="tables",
                   help="Limit to specific table(s). Repeatable. Default: all tables.")
    p.add_argument("--time-column", default=None,
                   help="Timestamp column. If omitted, a fixed value is used "
                        f"({DEFAULT_MISSING_TS}).")
    p.add_argument("--druid-url", default=DRUID_OVERLORD_URL,
                   help=f"Overlord URL (default: {DRUID_OVERLORD_URL}).")
    p.add_argument("--wait", action="store_true",
                   help="Poll task status until each finishes.")
    return p.parse_args()


def list_tables(namespace: str) -> list[str]:
    catalog = RestCatalog(
        CATALOG_NAME,
        **{"uri": CATALOG_URI, "warehouse": WAREHOUSE, **s3_properties()},
    )
    return [ident[-1] for ident in catalog.list_tables((namespace,))]


def build_spec(table: str, namespace: str, time_column: str | None) -> dict[str, Any]:
    timestamp_spec: dict[str, Any]
    if time_column:
        timestamp_spec = {"column": time_column, "format": "auto"}
    else:
        timestamp_spec = {
            "column": "__druid_missing_ts__",
            "format": "iso",
            "missingValue": DEFAULT_MISSING_TS,
        }

    return {
        "type": "index_parallel",
        "spec": {
            "dataSchema": {
                "dataSource": f"{namespace}__{table}",
                "timestampSpec": timestamp_spec,
                "dimensionsSpec": {
                    "useSchemaDiscovery": True,
                    "includeAllDimensions": True,
                    "dimensions": [],
                },
                "granularitySpec": {
                    "type": "uniform",
                    "segmentGranularity": "ALL",
                    "queryGranularity": "NONE",
                    "rollup": False,
                },
                "metricsSpec": [],
            },
            "ioConfig": {
                "type": "index_parallel",
                "inputSource": {
                    "type": "iceberg",
                    "tableName": table,
                    "namespace": [namespace],
                    "icebergCatalog": {
                        "type": "rest",
                        "catalogName": CATALOG_NAME,
                        "catalogProperties": {
                            "uri": INTRA_CATALOG_URI,
                            "warehouse": WAREHOUSE,
                            "s3.endpoint": INTRA_S3_ENDPOINT,
                            "s3.access-key-id": S3_ACCESS_KEY,
                            "s3.secret-access-key": S3_SECRET_KEY,
                            "s3.region": S3_REGION,
                            "s3.path-style-access": "true",
                        },
                    },
                    "icebergFilter": None,
                    "warehouseSource": {
                        "type": "s3",
                        "endpoint": INTRA_S3_ENDPOINT,
                    },
                },
                "inputFormat": {"type": "parquet"},
            },
            "tuningConfig": {
                "type": "index_parallel",
                "maxNumConcurrentSubTasks": 1,
            },
        },
    }


def submit_task(druid_url: str, spec: dict[str, Any]) -> str:
    r = requests.post(
        f"{druid_url}/druid/indexer/v1/task",
        json=spec, timeout=30,
    )
    r.raise_for_status()
    return r.json()["task"]


def task_status(druid_url: str, task_id: str) -> str:
    r = requests.get(f"{druid_url}/druid/indexer/v1/task/{task_id}/status", timeout=10)
    r.raise_for_status()
    return r.json()["status"]["statusCode"]


def wait_for(druid_url: str, task_id: str, table: str) -> None:
    while True:
        status = task_status(druid_url, task_id)
        if status in {"SUCCESS", "FAILED"}:
            color = "green" if status == "SUCCESS" else "red"
            console.print(f"  [{color}]{table}: {status}[/{color}]")
            return
        time.sleep(3)


def main() -> None:
    args = parse_args()
    tables = args.tables or list_tables(args.source_namespace)
    if not tables:
        console.print(f"[yellow]No tables found in namespace '{args.source_namespace}'[/yellow]")
        sys.exit(1)

    console.print(
        f"[cyan]Submitting[/cyan] {len(tables)} ingestion tasks from "
        f"namespace '[bold]{args.source_namespace}[/bold]' to {args.druid_url}"
    )
    submitted: list[tuple[str, str]] = []
    for t in tables:
        spec = build_spec(t, args.source_namespace, args.time_column)
        try:
            task_id = submit_task(args.druid_url, spec)
            submitted.append((t, task_id))
            console.print(f"  [green]submitted[/green] {t} -> task={task_id}")
        except requests.HTTPError as e:
            console.print(f"  [red]failed[/red] {t}: {e.response.text}")

    if args.wait:
        console.print("[cyan]Waiting for tasks to complete...[/cyan]")
        for t, task_id in submitted:
            wait_for(args.druid_url, task_id, t)


if __name__ == "__main__":
    main()
