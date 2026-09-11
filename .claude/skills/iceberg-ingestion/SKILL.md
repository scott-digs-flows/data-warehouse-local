---
name: iceberg-ingestion
description: How raw source files become Iceberg tables — the type mapping from source to Arrow to Iceberg, the pinned-schema contract, idempotent loading, known data hazards, and the checklist for onboarding a new source dataset such as Super Store or Stack Overflow. Use for any change to ingestion or to add a new data source.
---

# Raw files → Iceberg

This is the first half of the canonical pipeline and the layer everything else
depends on. Iceberg is where the data becomes engine-neutral; a mistake here
propagates to every engine at once.

The canonical loader is
`stacks/iceberg-multi-engine/scripts/load_iceberg.py`. Read it before changing
anything — it is the reference implementation of everything below.

## The pinned-schema contract

**Types are read from `shared/schemas/<Table>.json`, never inferred from CSV
text.** Each file carries the source's authoritative types from SQL Server's
`INFORMATION_SCHEMA`: `sql_type`, `precision`, `scale`, `nullable`, plus the
`source_name` → snake_case `name` mapping.

This is the single most important rule in the ingestion layer. CSV readers
infer differently — int32 vs int64, string vs date, decimal vs float — so
inference would hand each engine a subtly different view of the same bytes, and
any cross-engine discrepancy the BI app hit would be impossible to attribute.

A missing schema file is a hard error, not a reason to fall back to inference.

## Type mapping

SQL Server → Arrow, as implemented in `arrow_type()`:

| Source type | Arrow | Note |
| --- | --- | --- |
| `bit` | `bool_()` | |
| `tinyint`, `smallint`, `int` | `int32()` | **Iceberg has no 8- or 16-bit integer** — these widen |
| `bigint` | `int64()` | |
| `real` | `float32()` | |
| `float` | `float64()` | |
| `decimal`, `numeric` | `decimal128(p, s)` | Precision and scale from the pinned schema |
| `money` | `decimal128(19, 4)` | Never float — this is currency |
| `smallmoney` | `decimal128(10, 4)` | |
| `date` | `date32()` | |
| `datetime`, `datetime2`, `smalldatetime` | `timestamp("us")` | **Microseconds** — Iceberg rejects nanosecond precision |
| `datetimeoffset` | `timestamp("us", tz="UTC")` | |
| `time` | `time64("us")` | Untested — no source dataset has a `time` column yet. Do not assume it becomes String in ClickHouse: 26.7 has `Time`/`Time64` (DW-12) |
| anything else | `string()` | |

When adding a source with types not in this table, extend the mapping
deliberately and document the choice. Do not let an unmapped type fall through
to `string()` silently if it has a real Iceberg equivalent.

## Identifier normalisation

`PascalCase → snake_case`, applied to table and column names at the load
boundary by `snake_case()` in `_common.py`. It handles embedded acronyms:

```
DimCustomer          -> dim_customer
FactInternetSales    -> fact_internet_sales
CustomerPONumber     -> customer_po_number
UnitPriceDiscountPct -> unit_price_discount_pct
```

Normalising once here removes an entire class of cross-engine bug: Trino
lowercases, Postgres folds unquoted names, ClickHouse is case-sensitive,
Snowflake uppercases. **The raw CSV extract keeps the source's original
casing** — normalisation is a documented transformation on the way in, not a
rewrite of the source.

## Data hazards already known

These cost real debugging time. They are handled in the loader; preserve the
handling.

- **`bcp -k` writes some NULL `nvarchar` values as a literal NUL byte** (0x00)
  rather than an empty field — 287 rows each in `DimProduct`'s Spanish and
  French name columns. Parquet and Iceberg carry them fine; Postgres `text`
  rejects them outright. `strip_nul_bytes()` converts them to proper nulls at
  the load boundary, so every engine sees identical data rather than one
  consumer carrying a workaround.
- **MinIO intermittently 403s the first few object operations of a process**,
  then settles. Reproduced identically under s3fs and PyArrowFileIO and under
  two MinIO releases, so it is not a client bug. `with_s3_retry()` absorbs it —
  **only wrap genuinely idempotent operations in it.**
- **`bcp` writes NULL as an empty field for text columns too**, so
  `strings_can_be_null=True` is required in `ConvertOptions` or empty strings
  and NULLs become indistinguishable.

## Deliberate exclusions

Enforced in `_common.py`, not scattered through the loader:

- **`EXCLUDED_TABLES = {"DatabaseLog"}`** — a SQL Server DDL audit table, not
  part of the star schema. Its `XmlEvent` column has embedded newlines that
  `bcp` character format cannot round-trip; only 5 of 1,864 rows survived
  export intact.
- **`EXCLUDED_COLUMN_TYPES = {"varbinary", "binary", "image"}`** — three
  product/employee/territory photo blobs, ~18 MB across ~900 rows, no BI value,
  and they carry NUL bytes Postgres rejects.

Both are **documented narrowings of the source, not accidents**. Any new
exclusion needs the same treatment: enforced in one place, with the reason
written down.

## Loader contract

Any loader, for any source, must:

1. **Read pinned schemas.** Never infer.
2. **Be idempotent.** Re-running is always safe. The current loader drops and
   recreates each table; that is fine at this volume and is the honest way to
   guarantee it.
3. **Support `--table` for a subset.** Reloading one table must not require
   reloading 29.
4. **Collect failures and keep going**, then report every failure at the end
   and exit non-zero. A load that dies on table 3 of 29 tells you almost
   nothing.
5. **Report rows and elapsed time per table**, and a total.
6. **Write unpartitioned tables.** The largest here is under a million rows; a
   partition spec would add complexity and change nothing.

## Onboarding a new source dataset

For Super Store, Stack Overflow, or anything else. The pipeline is designed so
this is additive — no engine changes.

1. **Land the raw files** under `shared/data/<dataset_name>/`, snake_case
   directory. Keep them gitignored; they are reproducible.
2. **Produce pinned schemas** in `shared/schemas/` — one JSON per table, the
   same shape as the existing 29:

   ```json
   {
     "source_table": "FactInternetSales",
     "table": "fact_internet_sales",
     "columns": [
       {"source_name": "ProductKey", "name": "product_key", "sql_type": "int",
        "max_length": null, "precision": 10, "scale": 0, "nullable": false}
     ]
   }
   ```
   - From a database source, extract from its information schema, the way
     `shared/scripts/extract_source.py` does.
   - **From flat CSVs with no authoritative source**, you must still pin types
     rather than infer at load time: infer *once*, deliberately, review the
     result, and check the JSON in. The schema files become the contract.
     Record in the PR that these were inferred, not extracted.
3. **Choose a namespace.** `raw` holds AdventureWorks. A new dataset gets its
   own namespace (`super_store`, `stack_overflow`) — not a prefix inside `raw`.
4. **Extend the type mapping** if the source has types the table above does not
   cover.
5. **Load and verify.** Run the loader, then hand verification to the
   `data-validation` skill. Row counts against the source files, and spot-check
   types.
6. **Expose it to the engine** — see `clickhouse-warehouse`. A new namespace
   needs views created for it.
7. **Update the contract.** Add the dataset to `engines.yaml` and note it in
   the stack README and `CLAUDE.md`. The BI app discovers datasets there.

Expect to reuse the existing loader rather than write a new one. If a second
loader appears, the two will drift — factor the shared logic instead.

## Running it

```bash
cd stacks/iceberg-multi-engine
docker compose up -d                      # MinIO + Lakekeeper + Postgres
uv run python scripts/load_iceberg.py     # all tables
uv run python scripts/load_iceberg.py --table DimCustomer --table DimDate
```

Requires `127.0.0.1 lakekeeper` and `127.0.0.1 minio` in `/etc/hosts` — see
`stack-operations`. Without them, host-side scripts fail in confusing ways.
