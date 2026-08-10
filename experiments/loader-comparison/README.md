# Parked: loader comparison

Times CSV → Iceberg conversion across PyIceberg, DuckDB, and DataFusion, writing
each loader to its own namespace (`pyiceberg`, `duckdb`, `datafusion`).

**This is parked, and does not run as-is.** It is kept because the per-loader
code is a reasonable reference for what each library's write path looks like.

## Why it was parked

It answered a question this project is no longer asking, and it answered it
poorly:

- Two of the three "loaders" performed the *identical* Iceberg write —
  `load_datafusion.py` uses PyIceberg as its write shim, so the comparison was
  really DataFusion's CSV reader versus PyArrow's, not DataFusion versus
  PyIceberg.
- AdventureWorks is far too small to differentiate anything. Timings were
  dominated by CSV parsing and process overhead.
- Nothing here exercised the parts of Iceberg where tools actually diverge:
  partitioning, schema evolution, MERGE/upserts, compaction, snapshot expiry.

## To revive it

1. `uv sync --extra experiments` (pulls in `datafusion` and `pandas`).
2. These modules import `_common` and `_results` as top-level names, so they
   need `scripts/` on `sys.path` — or copy the helpers in here.
3. `_common.REPORTS_DIR` was removed when `reports/` was dropped; re-add it or
   point the benchmark output somewhere else.
4. Expect the three loaders to produce *different schemas* for the same CSV —
   they each infer types independently. The main tree solved this with pinned
   schemas in `schemas/*.json`; this experiment predates that.

If the goal is a real writer comparison, start from
[the main loader](../../scripts/load_iceberg.py) with pinned schemas, add Spark
as a reference implementation, and measure bytes/file counts/row-group sizes
alongside wall clock — not wall clock alone.
