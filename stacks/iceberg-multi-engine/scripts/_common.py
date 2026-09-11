"""Shared configuration and helpers."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, TypeVar

from dotenv import load_dotenv

# This stack lives at stacks/iceberg-multi-engine/; the raw layer is shared
# across stacks at the repo root.
STACK_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = STACK_ROOT.parent.parent
load_dotenv(STACK_ROOT / ".env", override=False)

DATA_DIR = REPO_ROOT / os.environ.get("AW_DATA_DIR", "shared/data/adventure_works_dw").lstrip("./")
SCHEMA_DIR = REPO_ROOT / "shared" / "schemas"

CATALOG_NAME = os.environ.get("ICEBERG_CATALOG_NAME", "adventure_works_dw")
CATALOG_URI = os.environ.get("ICEBERG_CATALOG_URI", "http://localhost:8181/catalog")
WAREHOUSE = os.environ.get("ICEBERG_WAREHOUSE", "adventure_works_dw")
NAMESPACE = os.environ.get("ICEBERG_NAMESPACE", "raw")

S3_ENDPOINT = os.environ.get("ICEBERG_S3_ENDPOINT", "http://localhost:9000")
S3_REGION = os.environ.get("ICEBERG_S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.environ.get("MINIO_ROOT_USER", "admin")
S3_SECRET_KEY = os.environ.get("MINIO_ROOT_PASSWORD", "admin12345")

# Excluded from the lake entirely: a SQL Server DDL audit table, not part of the
# star schema. Its XmlEvent column contains embedded newlines that bcp's
# character format cannot round-trip, so the CSV extract is unusable anyway.
EXCLUDED_TABLES = {"DatabaseLog"}

# Binary columns are dropped at load time: three product/employee/territory
# photo blobs that account for ~18 MB across ~900 rows, carry no BI value, and
# contain NUL bytes that Postgres text columns reject outright. Dropping them is
# a deliberate, documented narrowing of the source — not an accident.
EXCLUDED_COLUMN_TYPES = {"varbinary", "binary", "image"}

MAX_ATTEMPTS = 5
RETRY_BACKOFF_S = 0.5

T = TypeVar("T")


def with_s3_retry(fn: Callable[[], T], what: str) -> T:
    """Run an idempotent S3-backed operation, retrying transient auth failures.

    MinIO intermittently rejects the first few object operations of a process
    with 403 (surfaced as PermissionError), then settles. Reproduced identically
    under both s3fs and PyArrowFileIO, and under two MinIO releases, so it is
    not a client-library bug — the failures cluster at process start and vanish
    after a few operations.

    Callers must only pass operations that are safe to repeat.

    For *reads*, the closure must re-enter catalog.load_table() on every attempt:

        with_s3_retry(lambda: catalog.load_table(ident).scan().count(), name)

    Lakekeeper vends per-table S3 signing config (remote signing) that is bound to
    the Table object it was loaded into. Retrying a *cached* table's .scan() never
    recovers — it fails identically forever (measured: 15 consecutive 403s) —
    while re-loading the table settles within a few attempts. Hoisting the
    load_table() call out of the closure to "avoid redundant work" reintroduces
    the hang, so keep it inside.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fn()
        except PermissionError as e:
            last = e
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"{MAX_ATTEMPTS} attempts failed for {what}") from last

_ACRONYM_BOUNDARY = re.compile(r"(.)([A-Z][a-z]+)")
_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    """PascalCase -> snake_case, handling embedded acronyms.

        DimCustomer          -> dim_customer
        FactInternetSales    -> fact_internet_sales
        CustomerPONumber     -> customer_po_number
        UnitPriceDiscountPct -> unit_price_discount_pct

    Applied to table and column names on the way into Iceberg. Engines disagree
    about identifier case folding (Trino lowercases, Postgres folds unquoted
    names, ClickHouse is case-sensitive, Snowflake uppercases); normalising once
    at load time removes that entire class of cross-engine bug.
    """
    s = _ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    s = _WORD_BOUNDARY.sub(r"\1_\2", s)
    return s.lower()


def s3_properties() -> dict[str, str]:
    return {
        "s3.endpoint": S3_ENDPOINT,
        "s3.access-key-id": S3_ACCESS_KEY,
        "s3.secret-access-key": S3_SECRET_KEY,
        "s3.region": S3_REGION,
        "s3.path-style-access": "true",
    }


def list_csv_files() -> list[Path]:
    """Source CSVs, excluding tables we deliberately keep out of the lake."""
    return sorted(
        p for p in DATA_DIR.glob("*.csv")
        if p.is_file() and p.stem not in EXCLUDED_TABLES
    )
