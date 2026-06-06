"""Shared helpers used by the loader scripts."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=False)

DATA_DIR = REPO_ROOT / os.environ.get("AW_DATA_DIR", "data/adventure_works_dw").lstrip("./")
REPORTS_DIR = REPO_ROOT / "reports"

CATALOG_NAME = os.environ.get("ICEBERG_CATALOG_NAME", "adventure_works_dw")
CATALOG_URI = os.environ.get("ICEBERG_CATALOG_URI", "http://localhost:8181/catalog")
WAREHOUSE = os.environ.get("ICEBERG_WAREHOUSE", "adventure_works_dw")

S3_ENDPOINT = os.environ.get("ICEBERG_S3_ENDPOINT", "http://localhost:9000")
S3_REGION = os.environ.get("ICEBERG_S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.environ.get("ICEBERG_S3_ACCESS_KEY", "admin")
S3_SECRET_KEY = os.environ.get("ICEBERG_S3_SECRET_KEY", "admin12345")


def s3_properties() -> dict[str, str]:
    return {
        "s3.endpoint": S3_ENDPOINT,
        "s3.access-key-id": S3_ACCESS_KEY,
        "s3.secret-access-key": S3_SECRET_KEY,
        "s3.region": S3_REGION,
        "s3.path-style-access": "true",
    }


def list_csv_files() -> list[Path]:
    return sorted(p for p in DATA_DIR.glob("*.csv") if p.is_file())
