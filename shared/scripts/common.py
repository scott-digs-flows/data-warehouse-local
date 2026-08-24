"""Shared raw-layer helpers, used by every stack.

The raw layer is deliberately plain: CSV files plus pinned column types in
`shared/schemas/*.json`. No services, no running processes, no format lock-in —
any engine can be loaded from these two artifacts with a small per-stack script.

That, not Iceberg, is what keeps future engines cheap to add. Iceberg buys
zero-copy sharing between engines running *at the same time*; when you run one
engine at a time, a typed extract does the same job for free.

Import from a stack with:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "scripts"))
    import common
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = REPO_ROOT / "shared"
DATA_DIR = SHARED_DIR / "data" / "adventure_works_dw"
SCHEMA_DIR = SHARED_DIR / "schemas"

# Excluded from every stack: a SQL Server DDL audit table, not part of the star
# schema. Its XmlEvent column contains embedded newlines that bcp's character
# format cannot round-trip, so the CSV extract is unusable anyway.
EXCLUDED_TABLES = {"DatabaseLog"}

# Binary columns are dropped at load time: three product/employee/territory
# photo blobs, ~18 MB across ~900 rows, no BI value, and they carry NUL bytes
# that some engines reject outright.
EXCLUDED_COLUMN_TYPES = {"varbinary", "binary", "image"}

_ACRONYM_BOUNDARY = re.compile(r"(.)([A-Z][a-z]+)")
_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    """PascalCase -> snake_case, handling embedded acronyms.

        DimCustomer          -> dim_customer
        FactInternetSales    -> fact_internet_sales
        CustomerPONumber     -> customer_po_number
        UnitPriceDiscountPct -> unit_price_discount_pct

    Applied on the way into every engine. Engines disagree about identifier case
    folding, so normalising once at load time removes a whole class of bug.
    """
    s = _ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    s = _WORD_BOUNDARY.sub(r"\1_\2", s)
    return s.lower()


def list_csv_files() -> list[Path]:
    """Source CSVs, excluding tables deliberately kept out of every stack."""
    return sorted(
        p for p in DATA_DIR.glob("*.csv")
        if p.is_file() and p.stem not in EXCLUDED_TABLES
    )


def load_schema(csv_path: Path) -> dict:
    """Pinned column metadata for one table, written by extract_source.py.

    Loaders read these rather than inferring types from CSV text: inference
    disagrees between readers (int32 vs int64, string vs date, decimal
    handling), which would give each engine a subtly different view of the same
    data and make any cross-engine difference impossible to attribute.
    """
    path = SCHEMA_DIR / f"{csv_path.stem}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no pinned schema at {path}. Run shared/scripts/extract_source.py."
        )
    return json.loads(path.read_text())


def wanted_columns(spec: dict) -> list[dict]:
    """Schema columns minus the excluded binary blobs."""
    return [c for c in spec["columns"] if c["sql_type"] not in EXCLUDED_COLUMN_TYPES]
