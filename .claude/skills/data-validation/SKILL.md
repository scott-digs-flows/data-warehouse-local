---
name: data-validation
description: Verifying that data in Iceberg and the warehouse engines faithfully matches the source — row counts, type fidelity, null handling, star-schema referential integrity, and cross-engine agreement. Use after any load, loader change, or engine wiring change, and to investigate a suspected data discrepancy.
---

# Validating the warehouse

The claim this project makes is that **every engine sees the same data, and that
data matches the source**. Validation is what turns that from an assertion into
a fact.

## Principles

**The loader's success message is not evidence.** It reports what it believes it
inserted. Query the engine independently and confirm what is actually there.

**Ground truth is `shared/data/*.csv` plus `shared/schemas/*.json`** — not the
previous run, not another engine. A count that matches yesterday but not the
source is still wrong.

**Row counts are the weakest check.** They pass while a decimal arrives as a
float, a date as a string, or a NULL as an empty string — each of which breaks
the same-data-everywhere claim invisibly. Check types.

**Never draw performance conclusions.** ~1M rows total; every engine answers
instantly. Timings from this dataset are meaningless.

## The checks

### 1. Row counts against source

The source count is the CSV's line count minus its header:

```bash
cd shared/data/adventure_works_dw
for f in *.csv; do echo "$f $(( $(wc -l < "$f") - 1 ))"; done
```

Compare against the engine. Beware: a CSV with embedded newlines in a quoted
field will not match `wc -l` — `DatabaseLog` is excluded from the lake for
exactly this reason. For a trustworthy count, parse rather than count lines:

```bash
uv run python -c "
import pyarrow.csv as pacsv, sys
print(pacsv.read_csv(sys.argv[1]).num_rows)" shared/data/adventure_works_dw/DimCustomer.csv
```

Expected totals for AdventureWorks: **1,060,715 rows across 29 tables**;
`fact_internet_sales` is **60,398**.

### 2. Cross-engine agreement

The built-in check, and the best single command:

```bash
cd stacks/iceberg-multi-engine
uv run python scripts/smoke_test.py
uv run python scripts/smoke_test.py --engine clickhouse   # restrict to what is running
```

Expected:

```
✓ All engines agree on 60,398 rows
✓ All engines agree on the aggregate (10 territory groups, sums matched to 2dp)
```

Restrict `--engine` to the engines actually up, or failures will be about
connectivity rather than data.

### 3. Type fidelity

The check row counts cannot make. Compare what the engine reports against the
pinned schema:

```sql
-- ClickHouse: actual types as stored
SELECT name, type FROM system.columns
WHERE database = 'raw' AND table = 'fact_internet_sales'
ORDER BY position;
```

Look specifically for:

- **Currency as `Decimal`, never `Float`.** `money` → `Decimal(19, 4)`.
  A float here is a correctness bug, not a rounding preference.
- **Dates as a date type**, not String.
- **Timestamps at microsecond precision.** Iceberg rejects nanoseconds.
- **Nullability matching** the pinned schema's `nullable` flag.
- **`time` columns as String in ClickHouse** — expected, it has no native
  time-of-day type. Not a bug; confirm it is not a surprise.

Cross-check against the source of truth:

```bash
uv run python -c "
import json,pathlib
s=json.loads(pathlib.Path('shared/schemas/FactInternetSales.json').read_text())
for c in s['columns']:
    print(c['name'], c['sql_type'], c.get('precision'), c.get('scale'), c['nullable'])"
```

### 4. Null handling

The known hazard: `bcp -k` wrote some NULL `nvarchar` values as a literal NUL
byte — 287 rows each in `DimProduct`'s Spanish and French name columns. These
should arrive as **proper NULLs**, not empty strings and not 0x00.

```sql
SELECT
  countIf(spanish_product_name IS NULL)  AS nulls,
  countIf(spanish_product_name = '')     AS empty_strings,
  countIf(position(spanish_product_name, '\0') > 0) AS nul_bytes
FROM raw.dim_product;
```

`nul_bytes` must be 0. Empty strings and NULLs must be distinguishable — if
everything is an empty string, `strings_can_be_null=True` was lost from the
loader's `ConvertOptions`.

### 5. Referential integrity

Nothing enforces foreign keys here, so the star schema can silently degrade.
Every fact row should find its dimension:

```sql
-- Orphaned fact rows: should be 0
SELECT count() FROM raw.fact_internet_sales f
LEFT JOIN raw.dim_customer c ON f.customer_key = c.customer_key
WHERE c.customer_key IS NULL;
```

Worth knowing about the schema when writing these:

- **`*_alternate_key` is a natural/business key, not a join.**
- **`parent_*_key` is a self-referencing hierarchy** — 4 dimensions have one.
- **`fact_*.<name>_key` is a degenerate key** — the fact's own surrogate PK,
  not a dimension reference.
- **Role-playing dimensions:** `fact_internet_sales` joins `dim_date` three
  times (`order_date_key`, `due_date_key`, `ship_date_key`). Validate each.

### 6. Idempotency

Re-running a loader must be safe and must not change results:

```bash
uv run python scripts/load_iceberg.py --table DimCustomer   # twice
# row count identical both times — no duplication, no loss
```

## After a load

Minimum pass: counts against source, `smoke_test.py`, types on one fact and one
dimension, and the null check on `dim_product`.

After a **loader change** or a **new source dataset**, run everything, and
include the idempotency check.

## Reporting

Lead with the verdict: clean, or N problems. Per finding — expected, observed,
the command that shows it, and how far the cause was traced. List the passing
checks compactly so the reader knows the coverage. Do not fix what you find;
diagnose it precisely and hand it over.
