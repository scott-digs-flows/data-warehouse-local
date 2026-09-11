"""Re-check the lake's correctness invariants against the source files.

Separate from smoke_test.py on purpose. That script is the reference
implementation of what the BI app does — read engines.yaml, connect with each
engine's driver, compare answers — and the app has no access to
`shared/data/*.csv` or `shared/schemas/*.json`. Reconciling the lake against
source files there would make it a worse example of the thing it exists to
demonstrate. Different question, different tool:

    smoke_test.py   do the engines agree, and can a client reach them?
    verify_lake.py  does what is in the lake match what came out of SQL Server?

Both matter. Only this one would have caught DW-18, DW-19 or DW-20, each of
which was invisible to a green `load_iceberg.py` run because the loader reports
row counts from the in-memory Arrow table and never reads back.

**The source is the oracle.** Expected values are derived from the CSVs on every
run rather than hardcoded, so the checks do not rot when the data changes. The
one exception is `known_baseline`, which asserts the documented totals precisely
so that drift in the *source* is also caught.

Usage:
    uv run python scripts/verify_lake.py
    uv run python scripts/verify_lake.py --check null_reconciliation --check types
    uv run python scripts/verify_lake.py --strict      # skipped checks fail too
    uv run python scripts/verify_lake.py --list
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from pyiceberg.catalog.rest import RestCatalog
from rich.console import Console
from rich.table import Table as RichTable

import smoke_test
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

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Documented totals. Deliberately hardcoded — every other check derives its
# expectation from the source, so nothing would notice if the source itself
# changed. This one would.
KNOWN_ROWS, KNOWN_TABLES, KNOWN_COLUMNS = 1_060_715, 29, 343


@dataclass
class Result:
    status: str
    detail: str
    notes: list[str] = field(default_factory=list)


@dataclass
class SourceTable:
    """Everything we need from one CSV, computed in a single pass."""
    name: str
    rows: int
    empty_fields: dict[str, int]            # column -> count of genuinely empty fields
    token_counts: dict[str, dict[str, int]]  # column -> {literal value -> count}
    decimal_sums: dict[str, Decimal]
    nul_only_fields: dict[str, int]          # column -> fields that are exactly 0x00


@dataclass
class LakeTable:
    name: str
    rows: int
    nulls: dict[str, int]
    empty_strings: dict[str, int]
    nul_bytes: dict[str, int]
    decimal_sums: dict[str, Decimal]
    arrow: pa.Table


# Literal values worth counting in the source, because a loader is liable to
# mistake them for NULL. The PyArrow default null_values list is exactly this
# trap — see DW-18 and the `null_values=[""]` in load_iceberg.py.
WATCHED_TOKENS = ("NA", "N/A", "null", "NULL", "NaN", "nan", "n/a", "#N/A")


def pinned(stem: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{stem}.json").read_text())


def wanted(spec: dict) -> list[dict]:
    return [c for c in spec["columns"] if c["sql_type"] not in EXCLUDED_COLUMN_TYPES]


def read_source() -> dict[str, SourceTable]:
    """One pass over every CSV, read as raw text so nothing is inferred.

    `null_values=[]` and `strings_can_be_null=False` matter: this must see the
    bytes as bcp wrote them, not PyArrow's opinion of them. That is the whole
    point — the loader's interpretation is what we are checking.
    """
    out: dict[str, SourceTable] = {}
    for path in list_csv_files():
        spec = pinned(path.stem)
        cols = wanted(spec)
        tbl = pacsv.read_csv(
            path,
            convert_options=pacsv.ConvertOptions(
                column_types={c["source_name"]: pa.string() for c in cols},
                include_columns=[c["source_name"] for c in cols],
                strings_can_be_null=False,
                null_values=[],
            ),
        ).rename_columns([c["name"] for c in cols])

        empty, tokens, sums, nul_only = {}, {}, {}, {}
        for c in cols:
            col = tbl.column(c["name"])
            empty[c["name"]] = pc.sum(pc.cast(pc.equal(col, ""), pa.int64())).as_py() or 0
            nul_only[c["name"]] = pc.sum(pc.cast(pc.equal(col, "\x00"), pa.int64())).as_py() or 0
            counts = {}
            for tok in WATCHED_TOKENS:
                n = pc.sum(pc.cast(pc.equal(col, tok), pa.int64())).as_py() or 0
                if n:
                    counts[tok] = n
            if counts:
                tokens[c["name"]] = counts
            if c["sql_type"] in ("money", "smallmoney", "decimal", "numeric"):
                sums[c["name"]] = sum(
                    (Decimal(v) for v in col.to_pylist() if v not in ("", None)), Decimal(0)
                )
        out[spec["table"]] = SourceTable(
            spec["table"], tbl.num_rows, empty, tokens, sums, nul_only
        )
    return out


def read_lake(catalog: RestCatalog) -> dict[str, LakeTable]:
    """One pass over every Iceberg table.

    load_table() is re-entered inside every retry: Lakekeeper binds vended S3
    signing config to the Table object, so retrying a cached table's scan never
    recovers from MinIO's initial 403s. See with_s3_retry's docstring.
    """
    out: dict[str, LakeTable] = {}
    for path in list_csv_files():
        spec = pinned(path.stem)
        name = spec["table"]
        tbl = with_s3_retry(
            lambda n=name: catalog.load_table((NAMESPACE, n)).scan().to_arrow(), name
        )
        nulls, empties, nulb, sums = {}, {}, {}, {}
        for f in tbl.schema:
            col = tbl.column(f.name)
            nulls[f.name] = pc.sum(pc.is_null(col)).as_py() or 0
            if pa.types.is_string(f.type):
                empties[f.name] = pc.sum(pc.cast(pc.equal(col, ""), pa.int64())).as_py() or 0
                nulb[f.name] = pc.sum(
                    pc.cast(pc.match_substring(col, "\x00"), pa.int64())
                ).as_py() or 0
            if pa.types.is_decimal(f.type):
                sums[f.name] = sum(
                    (v for v in col.to_pylist() if v is not None), Decimal(0)
                )
        out[name] = LakeTable(name, tbl.num_rows, nulls, empties, nulb, sums, tbl)
    return out


# --------------------------------------------------------------------------
# Checks. Each takes the two summaries and returns a Result.
# --------------------------------------------------------------------------

def check_known_baseline(src, lake, ctx) -> Result:
    """The documented totals, asserted precisely.

    Every other check compares the lake against the source, so all of them would
    still pass if the source itself drifted. This one would not.
    """
    rows = sum(t.rows for t in lake.values())
    cols = sum(len(t.arrow.schema) for t in lake.values())
    bad = []
    if rows != KNOWN_ROWS: bad.append(f"rows {rows:,} != {KNOWN_ROWS:,}")
    if len(lake) != KNOWN_TABLES: bad.append(f"tables {len(lake)} != {KNOWN_TABLES}")
    if cols != KNOWN_COLUMNS: bad.append(f"columns {cols} != {KNOWN_COLUMNS}")
    if bad:
        return Result(FAIL, "; ".join(bad))
    return Result(PASS, f"{rows:,} rows, {len(lake)} tables, {cols} columns")


def check_row_counts(src, lake, ctx) -> Result:
    """Per-table counts, source vs lake. CSVs counted by parsing, never wc -l."""
    bad = [f"{n}: lake={lake[n].rows:,} csv={src[n].rows:,}"
           for n in src if lake[n].rows != src[n].rows]
    if bad:
        return Result(FAIL, f"{len(bad)} table(s) differ", bad[:10])
    return Result(PASS, f"all {len(src)} tables match ({sum(t.rows for t in src.values()):,} rows)")


def check_types(src, lake, ctx) -> Result:
    """Every non-binary column against the pinned schema."""
    bad = []
    for path in list_csv_files():
        spec = pinned(path.stem)
        fields = {f.name: f for f in ctx["catalog"].load_table(
            (NAMESPACE, spec["table"])).schema().fields}
        for c in wanted(spec):
            got = str(fields[c["name"]].field_type)
            exp = expected_iceberg_type(c)
            if got != exp:
                bad.append(f"{spec['table']}.{c['name']}: {c['sql_type']} -> {got}, want {exp}")
    if bad:
        return Result(FAIL, f"{len(bad)} mismatch(es)", bad[:10])
    n = sum(len(wanted(pinned(p.stem))) for p in list_csv_files())
    return Result(PASS, f"all {n} columns match the pinned schemas")


def expected_iceberg_type(c: dict) -> str:
    t, p, s = c["sql_type"], c["precision"], c["scale"]
    if t == "bit": return "boolean"
    if t in ("tinyint", "smallint", "int"): return "int"   # Iceberg has no 8/16-bit int
    if t == "bigint": return "long"
    if t == "real": return "float"
    if t == "float": return "double"
    if t in ("decimal", "numeric"): return f"decimal({p or 38}, {s or 0})"
    if t == "money": return "decimal(19, 4)"
    if t == "smallmoney": return "decimal(10, 4)"
    if t == "date": return "date"
    if t in ("datetime", "datetime2", "smalldatetime"): return "timestamp"
    if t == "datetimeoffset": return "timestamptz"
    if t == "time": return "time"
    return "string"


# Every branch of expected_iceberg_type(), including the 8 with no data in this
# dataset. (sql_type, precision, scale) -> expected Iceberg type.
TYPE_MAPPING_CASES = [
    ("bit", None, None, "boolean"),
    ("tinyint", 3, 0, "int"),        # Iceberg has no 8-bit int
    ("smallint", 5, 0, "int"),       # nor 16-bit
    ("int", 10, 0, "int"),
    ("bigint", 19, 0, "long"),       # no data here
    ("real", 24, None, "float"),
    ("float", 53, None, "double"),
    ("decimal", 18, 4, "decimal(18, 4)"),   # no data here
    ("numeric", 9, 2, "decimal(9, 2)"),     # no data here
    ("decimal", None, None, "decimal(38, 0)"),
    ("money", 19, 4, "decimal(19, 4)"),
    ("smallmoney", 10, 4, "decimal(10, 4)"),  # no data here
    ("date", None, None, "date"),
    ("datetime", None, None, "timestamp"),
    ("datetime2", None, None, "timestamp"),
    ("smalldatetime", None, None, "timestamp"),
    ("datetimeoffset", None, None, "timestamptz"),  # no data here
    ("time", None, None, "time"),                   # no data here
    ("nvarchar", None, None, "string"),
    ("nchar", None, None, "string"),
    ("varchar", None, None, "string"),
    ("char", None, None, "string"),
    ("xml", None, None, "string"),                  # falls through to string
]


def check_type_mapping(src, lake, ctx) -> Result:
    """Pin every branch of expected_iceberg_type(), including the unexercised ones.

    This dataset has no bigint, decimal/numeric, smallmoney, datetime2,
    smalldatetime, datetimeoffset, time or xml columns, so several of the branches below are never reached by check_types.
    They could drift from the intended mapping and nothing would notice until a
    source dataset that uses them arrived.

    **What this buys and what it does not.** It pins the *duplicate* against
    intended behaviour — NOT against the loader. If load_iceberg.arrow_type
    changed, this check would still pass while check_types failed, and that is
    the correct division: expected_iceberg_type() deliberately restates the
    mapping so check_types is not comparing the implementation to itself.
    Anyone tempted to "simplify" this by importing arrow_type would destroy the
    only independent statement of what the types are supposed to be.
    """
    # Tie the case list to the function's actual branches. Without this, adding
    # a branch to expected_iceberg_type() and forgetting to pin it leaves this
    # check green — review proved it by adding `uniqueidentifier -> int` unnoticed.
    import ast as _ast, inspect as _inspect
    branches: set[str] = set()
    for node in _ast.walk(_ast.parse(_inspect.getsource(expected_iceberg_type))):
        if isinstance(node, _ast.Compare) and isinstance(node.left, _ast.Name) \
                and node.left.id == "t":
            for c in node.comparators:
                if isinstance(c, _ast.Constant):
                    branches.add(c.value)
                elif isinstance(c, (_ast.Tuple, _ast.List)):
                    branches.update(e.value for e in c.elts if isinstance(e, _ast.Constant))
    unpinned = sorted(branches - {c[0] for c in TYPE_MAPPING_CASES})
    bad = [f"branch '{b}' exists in expected_iceberg_type() but is not pinned"
           for b in unpinned]
    for sql_type, precision, scale, want in TYPE_MAPPING_CASES:
        got = expected_iceberg_type(
            {"sql_type": sql_type, "precision": precision, "scale": scale}
        )
        if got != want:
            bad.append(f"{sql_type}(p={precision},s={scale}): got {got}, want {want}")
    if bad:
        return Result(FAIL, f"{len(bad)} of {len(TYPE_MAPPING_CASES)} branches drifted", bad[:10])
    live = {c["sql_type"] for p in list_csv_files() for c in wanted(pinned(p.stem))}
    unexercised = sorted({c[0] for c in TYPE_MAPPING_CASES} - live)
    return Result(PASS, f"all {len(TYPE_MAPPING_CASES)} mapping branches hold",
                  [f"{len(unexercised)} have no data in this dataset and are pinned only here: "
                   f"{', '.join(unexercised)}",
                   "pins the duplicate against intent, NOT against load_iceberg.arrow_type"])


def check_nullability_flags(src, lake, ctx) -> Result:
    """Pinned NOT NULL must mean Iceberg `required`, column for column."""
    req = opt = 0
    bad = []
    for path in list_csv_files():
        spec = pinned(path.stem)
        fields = {f.name: f for f in ctx["catalog"].load_table(
            (NAMESPACE, spec["table"])).schema().fields}
        for c in wanted(spec):
            f = fields[c["name"]]
            req, opt = (req + 1, opt) if f.required else (req, opt + 1)
            if f.required != (not c["nullable"]):
                bad.append(f"{spec['table']}.{c['name']}: required={f.required} "
                           f"pinned_nullable={c['nullable']}")
    if bad:
        return Result(FAIL, f"{len(bad)} mismatch(es)", bad[:10])
    return Result(PASS, f"{req} required / {opt} optional, matching the pinned flags")


def check_nullability_enforced(src, lake, ctx) -> Result:
    """Prove the constraint still REJECTS a NULL, not merely that flags are set.

    Exercised in-process against the loader's own function rather than by writing
    to the lake: a verification suite must not mutate what it verifies.
    """
    import load_iceberg
    cols = [{"name": "k", "sql_type": "int", "precision": 10, "scale": 0, "nullable": False}]

    # Positive half: a clean table must cast AND come out actually non-nullable.
    # Without this, deleting the function's body would still "pass" the negative
    # half below, because an AttributeError looks like a rejection.
    try:
        ok = load_iceberg.apply_pinned_nullability(
            pa.table({"k": pa.array([1, 2, 3], pa.int32())}), cols)
    except Exception as e:
        return Result(FAIL, f"clean data was rejected: {type(e).__name__}: {e}")
    if ok.schema.field("k").nullable:
        return Result(FAIL, "pinned NOT NULL did not produce a non-nullable field")

    # Negative half: the catch is deliberately NARROW. Catching bare Exception
    # made a KeyError, an AttributeError or a NotImplementedError read as
    # "rejected", so drift in the function would have been reported as a pass.
    try:
        load_iceberg.apply_pinned_nullability(
            pa.table({"k": pa.array([1, None, 3], pa.int32())}), cols)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, ValueError) as e:
        return Result(PASS, f"NULL into a pinned NOT NULL column is rejected "
                            f"({type(e).__name__})",
                      ["tests the loader's cast in-process, not the lake's stored schema"])
    except Exception as e:
        return Result(FAIL, f"rejected, but not for the right reason: "
                            f"{type(e).__name__}: {e}")
    return Result(FAIL, "apply_pinned_nullability() ACCEPTED a NULL in a NOT NULL column")


def check_null_reconciliation(src, lake, ctx) -> Result:
    """The check that caught DW-18.

    For every string column, the lake's NULL count must equal the number of
    genuinely empty fields in the source. More NULLs in the lake means values
    were destroyed on the way in — which is exactly what pyarrow's default
    null_values list was doing to the literal string "NA".
    """
    bad = []
    for name, lt in lake.items():
        st = src[name]
        for col, n_lake in lt.nulls.items():
            if col not in lt.empty_strings:      # string columns only
                continue
            n_src = st.empty_fields.get(col, 0)
            if n_lake != n_src:
                bad.append(f"{name}.{col}: lake_nulls={n_lake} source_empty={n_src} "
                           f"({n_lake - n_src:+d})")
    n = sum(len(lt.empty_strings) for lt in lake.values())
    if bad:
        return Result(FAIL, f"{len(bad)} of {n} string columns differ", bad[:10])
    return Result(PASS, f"all {n} string columns reconcile")


def check_source_values(src, lake, ctx) -> Result:
    """DW-18 and DW-19 invariants, derived from the source so they cannot rot.

    DW-18: a literal token like "NA" in the source must survive as that string.
    DW-19: a field that is exactly one NUL byte must arrive as an empty string.
    """
    bad, checked = [], 0
    for name, st in src.items():
        arrow = lake[name].arrow
        for col, toks in st.token_counts.items():
            for tok, n_src in toks.items():
                checked += 1
                n_lake = pc.sum(pc.cast(pc.equal(arrow.column(col), tok), pa.int64())).as_py() or 0
                if n_lake != n_src:
                    bad.append(f"DW-18 {name}.{col} '{tok}': lake={n_lake} source={n_src}")
        for col, n_src in st.nul_only_fields.items():
            if not n_src:
                continue
            checked += 1
            n_lake = lake[name].empty_strings.get(col, 0)
            n_nul = lake[name].nul_bytes.get(col, 0)
            if n_lake != n_src or n_nul:
                bad.append(f"DW-19 {name}.{col}: source NCHAR(0)={n_src} "
                           f"lake_empty={n_lake} lake_nul_bytes={n_nul}")
    if bad:
        return Result(FAIL, f"{len(bad)} invariant(s) broken", bad[:10])
    return Result(PASS, f"{checked} source-derived value invariant(s) hold")


def check_value_digests(src, lake, ctx) -> Result:
    """Every cell of every column, compared — not just counts, types and flags.

    Without this the suite passes on wholesale value corruption: independent
    review replaced an entire string column, zeroed an entire int column, and
    shifted every date in `dim_date` by one day, and all ten other checks stayed
    green. 172 of 343 columns had no value-level check at all, and `dim_date` is
    the conformed dimension every fact joins to.

    Method: an order-independent digest per column — the summed hash of every
    value — computed from the source CSV parsed through the pinned schema, and
    from the lake. Order-independent because nothing guarantees the lake
    preserves CSV row order; a sort would cost more and buy little.

    Two honest limits, both stated in the run's NOT_CHECKED output:

    * A *compensating swap* — two rows exchanging values within one column —
      leaves the multiset unchanged and is invisible here, exactly as it is to
      `decimal_exactness`'s sums.
    * The source side is parsed with the loader's own `read_csv`, so this
      compares the lake against a *re-parse*, which detects corruption of the
      lake but not a bug in parsing itself. That is deliberate: parse bugs are
      what `null_reconciliation` and `source_values` catch, and they work from
      the raw CSV text precisely so they do not share this blind spot.
    """
    import load_iceberg
    bad, n_cols, n_cells = [], 0, 0
    for path in list_csv_files():
        spec = pinned(path.stem)
        name = spec["table"]
        want_tbl = load_iceberg.read_csv(path, spec)
        got_tbl = lake[name].arrow
        for col in want_tbl.schema.names:
            n_cols += 1
            cw, cg = want_tbl.column(col), got_tbl.column(col)
            n_cells += len(cw)
            if len(cw) != len(cg):
                bad.append(f"{name}.{col}: {len(cg)} values in the lake, {len(cw)} in source")
                continue
            # Fast path: a full-refresh load preserves CSV row order, so the
            # arrays are usually positionally equal and Arrow can settle it in
            # C++. Only fall back to the order-independent digest — which costs
            # a Python pass over every cell — when that fails, so an unexpected
            # reordering is still reported correctly rather than as a difference.
            if cw.equals(cg):
                continue
            w, g = cw.to_pylist(), cg.to_pylist()
            dw = sum(hash(v) for v in w) % (2 ** 63)
            dg = sum(hash(v) for v in g) % (2 ** 63)
            if dw != dg:
                diffs = [(i, a, b) for i, (a, b) in enumerate(zip(w, g)) if a != b][:2]
                where = "; ".join(f"row {i}: source={a!r} lake={b!r}" for i, a, b in diffs)
                bad.append(f"{name}.{col}: values differ ({where or 'multiset differs'})")
    if bad:
        return Result(FAIL, f"{len(bad)} of {n_cols} columns differ", bad[:10])
    return Result(PASS, f"all {n_cols} columns match value-for-value ({n_cells:,} cells)",
                  ["order-independent digest: a compensating swap within a column is not caught"])


def own_primary_key(table: str) -> str:
    """A table's own surrogate key: dim_customer -> customer_key."""
    stem = table[4:] if table.startswith("dim_") else table[5:] if table.startswith("fact_") else table
    return f"{stem}_key"


def schema_tables() -> set[str]:
    """Tables the pinned schemas say should exist — the classification universe.

    Deliberately not "tables currently in the lake": a dimension missing from the
    lake is a different failure from a key the naming convention cannot place,
    and deriving against lake contents conflated the two into a misleading
    "no dim_x" message.
    """
    return {pinned(p.stem)["table"] for p in list_csv_files()}


def derive_joins(tables: set[str] | None = None) -> tuple[list[tuple], list[tuple], list[str]]:
    """Work the foreign keys out from the pinned schemas rather than listing them.

    Hand-listing is how the previous version acquired a gap: it covered 6 joins
    and read as complete. Derivation covers 44 and, more importantly, *notices*
    when a new table brings a key it cannot classify.

    Every `*_key` column falls into one of five buckets. Four are deliberately
    not joins, and each is excluded by a stated rule rather than by omission:

    * `<table>_key` matching the table's own name — its surrogate primary key.
    * `*_alternate_key` — the natural/business key from the source system.
      `dim_product.product_alternate_key` is a product code, not a reference.
    * `parent_*_key` — a self-referencing hierarchy, checked separately because
      a NULL is valid at the root and a cycle is the real corruption.
    * A fact's own degenerate key, e.g. `fact_finance.finance_key`.

    Anything left resolves to `dim_<stem>`, or to `dim_date` for the
    role-playing date keys — `order_`, `due_` and `ship_` all point at the same
    dimension, and naming alone cannot tell you they are different roles.

    Returns (fk_edges, hierarchies, unclassified). A non-empty `unclassified`
    is a failure, not a footnote.
    """
    tables = tables if tables is not None else schema_tables()
    fks, hierarchies, unclassified = [], [], []
    for path in list_csv_files():
        spec = pinned(path.stem)
        t = spec["table"]
        for c in wanted(spec):
            n = c["name"]
            if not n.endswith("_key"):
                continue
            if n.startswith("parent_"):
                # The parent points at whatever the column is named after:
                # parent_employee_key -> employee_key (surrogate), and
                # parent_account_code_alternate_key -> account_code_alternate_key
                # (natural). Handling both here is why this is tested BEFORE the
                # _alternate_key rule — ordering it the other way silently
                # dropped the two natural-key hierarchies.
                target_col = n[len("parent_"):]
                cols_here = {x["name"] for x in wanted(spec)}
                hierarchies.append((t, n, target_col if target_col in cols_here
                                    else own_primary_key(t)))
                continue
            if n.endswith("_alternate_key"):
                continue
            if n == own_primary_key(t):
                continue
            stem = n[:-4]
            target = f"dim_{stem}"
            if target in tables and target != t:
                fks.append((t, n, target, own_primary_key(target)))
            elif stem.endswith("_date") and "dim_date" in tables:
                fks.append((t, n, "dim_date", "date_key"))
            elif t == f"fact_{stem}":
                continue                      # a fact's own degenerate key
            else:
                unclassified.append(f"{t}.{n} (no dim_{stem}, not a PK, not alternate)")
    return fks, hierarchies, unclassified


def derive_primary_keys() -> list[tuple[str, str]]:
    """Each table's own surrogate key, by the same naming rule as derive_joins."""
    out = []
    for path in list_csv_files():
        spec = pinned(path.stem)
        t = spec["table"]
        pk = own_primary_key(t)
        if any(c["name"] == pk for c in wanted(spec)):
            out.append((t, pk))
    return out


def check_primary_keys(src, lake, ctx) -> Result:
    """Surrogate keys must be unique.

    derive_joins already identifies these and discards them, and every FK check
    is a set-membership test — which cannot see a duplicate. A duplicated
    surrogate key would leave all 44 joins green while silently fanning out any
    join that touches it.
    """
    bad, checked = [], 0
    for t, pk in derive_primary_keys():
        if t not in lake:
            continue
        checked += 1
        vals = lake[t].arrow.column(pk).to_pylist()
        if len(vals) != len(set(vals)):
            dupes = {v for v in vals if vals.count(v) > 1} if len(vals) < 50_000 else set()
            bad.append(f"{t}.{pk}: {len(vals) - len(set(vals))} duplicate(s)"
                       + (f" e.g. {sorted(dupes)[:3]}" if dupes else ""))
        if any(v is None for v in vals):
            bad.append(f"{t}.{pk}: contains NULL")
    if bad:
        return Result(FAIL, f"{len(bad)} primary key problem(s)", bad[:10])
    return Result(PASS, f"{checked} surrogate keys unique and non-NULL")


def check_referential_integrity(src, lake, ctx) -> Result:
    """Every derived fact-to-dimension edge, including role-playing dates."""
    fks, _, unclassified = derive_joins()
    if unclassified:
        return Result(FAIL, f"{len(unclassified)} *_key column(s) could not be classified",
                      unclassified[:10] +
                      ["a key the derivation cannot place is an uncovered join, not a footnote"])
    bad, checked, compared, vacuous = [], 0, 0, []
    for ft, fk, dt, dk in fks:
        if ft not in lake or dt not in lake:
            bad.append(f"{ft}.{fk} -> {dt}: table missing from the lake")
            continue
        checked += 1
        dim = set(lake[dt].arrow.column(dk).to_pylist())
        vals = [v for v in lake[ft].arrow.column(fk).to_pylist() if v is not None]
        compared += len(vals)
        if not vals:
            # Not a failure — a nullable FK may legitimately be all NULL — but it
            # must not be counted as evidence. new_fact_currency_rate.date_key is
            # 0/50 non-null on clean data today.
            vacuous.append(f"{ft}.{fk} -> {dt}")
        orphans = {v for v in vals if v not in dim}
        if orphans:
            bad.append(f"{ft}.{fk} -> {dt}.{dk}: {len(orphans)} orphan key(s) "
                       f"e.g. {sorted(orphans)[:3]}")
    if bad:
        return Result(FAIL, f"{len(bad)} of {len(fks)} derived join(s) broken", bad[:10])
    notes = ["derived from the pinned schemas, not hand-listed; "
             "an unclassifiable *_key fails this check"]
    if vacuous:
        notes.append(f"{len(vacuous)} edge(s) had no non-NULL values and therefore "
                     f"tested nothing: {', '.join(vacuous)}")
    return Result(PASS, f"0 orphans across {checked} derived joins "
                        f"({compared:,} non-NULL values compared)", notes)


def check_hierarchies(src, lake, ctx) -> Result:
    """Self-referencing parent_*_key columns: dangling parents and cycles.

    Two distinct failures, and an orphan check alone only finds the first:

    * A non-NULL parent that does not exist in its own table's primary key.
      NULL is valid and expected — that is the root of the tree.
    * A cycle. Every row can have a parent that exists, and the structure still
      be corrupt, because following parents never terminates. That is invisible
      to a membership test and is why this is a separate check.
    """
    _, hierarchies, _ = derive_joins()
    bad, checked = [], 0
    for t, col, pk in hierarchies:
        if t not in lake:
            continue
        checked += 1
        arrow = lake[t].arrow
        parent = dict(zip(arrow.column(pk).to_pylist(), arrow.column(col).to_pylist()))
        keys = set(parent)
        dangling = {p for p in parent.values() if p is not None and p not in keys}
        if dangling:
            bad.append(f"{t}.{col}: {len(dangling)} parent(s) not in {pk} "
                       f"e.g. {sorted(dangling)[:3]}")
        for start in parent:                  # walk to a root or a repeat
            seen, node = set(), start
            while node is not None and node in parent:
                if node in seen:
                    bad.append(f"{t}.{col}: cycle reachable from {pk}={start} "
                               f"(revisits {node})")
                    break
                seen.add(node)
                node = parent[node]
            if bad and bad[-1].startswith(f"{t}.{col}: cycle"):
                break                         # one cycle report per column is enough
    if bad:
        return Result(FAIL, f"{len(bad)} hierarchy problem(s)", bad[:10])
    return Result(PASS, f"{checked} self-referencing hierarchies intact (no dangling parents, "
                        f"no cycles)", ["a NULL parent is the root and is not flagged"])


def check_decimal_exactness(src, lake, ctx) -> Result:
    """Sums from the lake vs the CSV text parsed as Decimal.

    Types alone do not prove exactness — this is what a silent float round-trip
    would fail. Exhaustive over every decimal column, not sampled.
    """
    bad, n = [], 0
    for name, st in src.items():
        for col, want in st.decimal_sums.items():
            got = lake[name].decimal_sums.get(col)
            n += 1
            if got is None:
                bad.append(f"{name}.{col}: not a decimal in the lake")
            elif Decimal(got) != want:
                bad.append(f"{name}.{col}: lake={got} source={want} diff={Decimal(got)-want}")
    if bad:
        return Result(FAIL, f"{len(bad)} of {n} decimal column(s) differ", bad[:10])
    return Result(PASS, f"all {n} decimal columns sum exactly (exhaustive, not sampled)")


def check_cross_engine(src, lake, ctx) -> Result:
    """Agreement between engines that are actually up.

    Reuses smoke_test's executors rather than reimplementing them — one
    implementation of "how to query engine X". Engines that are down are
    reported as skipped, never silently dropped.
    """
    import _manifest
    manifest = _manifest.load()
    probe = "fact_internet_sales"
    want = lake[probe].rows
    agree, absent, wrong = [], [], []
    for e in manifest.engines:
        runner = smoke_test.EXECUTORS.get(e.driver)
        if runner is None:
            absent.append(f"{e.name} (no executor for driver {e.driver})")
            continue
        try:
            rows = runner(e, f"SELECT COUNT(*) FROM {e.table_ref(probe)}")
            got = int(smoke_test.normalise(rows[0][0]))
        except Exception as exc:
            absent.append(f"{e.name} ({type(exc).__name__})")
            continue
        (agree if got == want else wrong).append(f"{e.name}={got:,}")
    if wrong:
        return Result(FAIL, f"engines disagree on {probe}: want {want:,}; " + ", ".join(wrong),
                      [f"agreed: {', '.join(agree)}"] if agree else [])
    if not agree:
        return Result(SKIP, "no engine reachable", [f"not reached: {a}" for a in absent])
    if absent and ctx.get("strict"):
        # --strict means "every engine the manifest advertises must answer".
        # Previously this only failed when ALL engines were down, so a missing
        # Postgres sync passed a --strict run green.
        return Result(FAIL, f"--strict: {len(absent)} manifest engine(s) did not answer",
                      [f"not reached: {a}" for a in absent] +
                      [f"agreed: {', '.join(agree)}"])
    notes = [f"not reached (reported, not ignored): {', '.join(absent)}"] if absent else []
    return Result(PASS, f"{len(agree)} engine(s) agree on {want:,} rows: " + ", ".join(agree), notes)


CHECKS: dict[str, Callable] = {
    "known_baseline": check_known_baseline,
    "row_counts": check_row_counts,
    "types": check_types,
    "type_mapping": check_type_mapping,
    "nullability_flags": check_nullability_flags,
    "nullability_enforced": check_nullability_enforced,
    "null_reconciliation": check_null_reconciliation,
    "source_values": check_source_values,
    "value_digests": check_value_digests,
    "primary_keys": check_primary_keys,
    "referential_integrity": check_referential_integrity,
    "hierarchies": check_hierarchies,
    "decimal_exactness": check_decimal_exactness,
    "cross_engine": check_cross_engine,
}

# Stated on every run. A suite that implies a passing run means "everything is
# verified" is worse than one that says where its edges are.
NOT_CHECKED = [
    "A compensating swap — two rows exchanging values within one column. Both "
    "value_digests and decimal_exactness compare multisets and sums, which such a "
    "swap leaves unchanged.",
    "Foreign keys the *_key naming convention cannot express. Two real ones exist here: "
    "fact_internet_sales_reason's composite (sales_order_number, sales_order_line_number) "
    "into fact_internet_sales, and new_fact_currency_rate.currency_id into "
    "dim_currency.currency_alternate_key. Both are clean today, neither is checked.",
    "Referential integrity counts EDGES, not rows, and skips NULL foreign keys — so a "
    "nullable FK that is entirely NULL passes while testing nothing. The run reports how "
    "many non-NULL values were actually compared, and names any edge that tested nothing.",
    "Cross-engine agreement beyond one COUNT(*) on one table. smoke_test.py also "
    "compares a grouped SUM; engines could disagree on every decimal and pass here.",
    "Whether the LAKE rejects a NULL in a required column. nullability_enforced tests "
    "the loader's cast in-process; nullability_flags tests the stored schema.",
    "Whether a source NULL differs from a source empty string — bcp writes both as an "
    "empty field, so the distinction is gone before the lake sees it (accepted, DECISIONS.md).",
    "That a GUI can browse the warehouse. Only the metadata queries a navigator issues "
    "are exercised, by smoke_test and by hand (DW-13).",
    "That the extract reproduces from SQL Server. That needs the ~10 min extract and a "
    "before/after comparison (DW-8); this suite starts from the CSVs as they are.",
    "Performance of anything. The dataset is far too small to support a conclusion.",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="append", dest="checks",
                   help="Run only these checks. Repeatable. Default: all.")
    p.add_argument("--strict", action="store_true",
                   help="Treat SKIP as failure — for a gate where an absent engine is a problem.")
    p.add_argument("--list", action="store_true", help="List check names and exit.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.list:
        for n in CHECKS:
            console.print(f"  {n}")
        return

    names = list(CHECKS)
    if args.checks:
        unknown = [c for c in args.checks if c not in CHECKS]
        if unknown:
            console.print(f"[red]Unknown check(s):[/red] {', '.join(unknown)}")
            console.print(f"Known: {', '.join(CHECKS)}")
            raise SystemExit(2)
        names = [c for c in CHECKS if c in args.checks]

    # The lake is this suite's subject, not an optional dependency. If it cannot
    # be reached there is nothing to verify, and saying so beats reporting a
    # tidy screen of skips — that is how a suite becomes decorative.
    try:
        # Construction, not just list_tables, must sit inside the try:
        # RestCatalog.__init__ fetches /catalog/v1/config, so an unreachable
        # catalog raises here. Building it outside made this handler dead code
        # and leaked a raw traceback instead of the guidance below.
        catalog = RestCatalog(
            CATALOG_NAME, **{"uri": CATALOG_URI, "warehouse": WAREHOUSE, **s3_properties()}
        )
        tables = catalog.list_tables((NAMESPACE,))
    except Exception as e:
        console.print(f"[bold red]Cannot reach the lake.[/bold red] {type(e).__name__}: {e}")
        console.print(
            f"\nThe catalog at [cyan]{CATALOG_URI}[/cyan] did not answer. Bring the stack up:\n"
            "  cd stacks/iceberg-multi-engine && docker compose --profile clickhouse up -d\n"
            "and check `127.0.0.1 lakekeeper` and `127.0.0.1 minio` are in /etc/hosts."
        )
        raise SystemExit(2)
    if not tables:
        console.print(f"[bold red]The `{NAMESPACE}` namespace is empty.[/bold red] "
                      "Run scripts/load_iceberg.py first.")
        raise SystemExit(2)

    t0 = time.perf_counter()
    console.print(f"[cyan]Reading source[/cyan] ({len(list_csv_files())} CSVs)…")
    src = read_source()
    t_src = time.perf_counter() - t0
    console.print(f"[cyan]Reading lake[/cyan] ({len(tables)} Iceberg tables)…")
    lake = read_lake(catalog)
    t_lake = time.perf_counter() - t0 - t_src

    ctx = {"catalog": catalog, "strict": args.strict}
    results: dict[str, tuple[Result, float]] = {}
    for name in names:
        s = time.perf_counter()
        try:
            r = CHECKS[name](src, lake, ctx)
        except Exception as e:      # a broken check is a failure, not a crash
            r = Result(FAIL, f"check raised {type(e).__name__}: {e}")
        results[name] = (r, time.perf_counter() - s)

    table = RichTable(show_header=True, header_style="bold")
    table.add_column("Check"); table.add_column("Result"); table.add_column("Detail")
    table.add_column("Time", justify="right")
    style = {PASS: "green", FAIL: "bold red", SKIP: "yellow"}
    for name, (r, secs) in results.items():
        table.add_row(name, f"[{style[r.status]}]{r.status}[/{style[r.status]}]",
                      r.detail, f"{secs:.2f}s")
    console.print()
    console.print(table)

    for name, (r, _) in results.items():
        for n in r.notes:
            console.print(f"  [dim]{name}:[/dim] {n}")

    failed = [n for n, (r, _) in results.items() if r.status == FAIL]
    skipped = [n for n, (r, _) in results.items() if r.status == SKIP]
    total = time.perf_counter() - t0

    console.print(f"\n[bold]Not checked by this suite[/bold] — a pass does not mean these hold:")
    for line in NOT_CHECKED:
        console.print(f"  [dim]•[/dim] {line}")

    console.print(
        f"\nsource read {t_src:.1f}s · lake read {t_lake:.1f}s · "
        f"checks {total - t_src - t_lake:.1f}s · [bold]total {total:.1f}s[/bold]"
    )
    if skipped:
        console.print(f"[yellow]{len(skipped)} skipped:[/yellow] {', '.join(skipped)}"
                      + ("  [red](--strict: counted as failure)[/red]" if args.strict else ""))
    if failed:
        console.print(f"[bold red]FAILED[/bold red] {len(failed)}/{len(results)}: "
                      f"{', '.join(failed)}")
        raise SystemExit(1)
    if skipped and args.strict:
        raise SystemExit(1)
    console.print(f"[bold green]All {len(results) - len(skipped)} checks passed.[/bold green]")


if __name__ == "__main__":
    main()
