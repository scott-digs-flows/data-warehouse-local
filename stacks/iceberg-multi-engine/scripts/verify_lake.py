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
    tbl = pa.table({"k": pa.array([1, None, 3], pa.int32())})
    try:
        load_iceberg.apply_pinned_nullability(tbl, cols)
    except Exception as e:
        return Result(PASS, f"a NULL in a pinned NOT NULL column is rejected ({type(e).__name__})")
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


def check_referential_integrity(src, lake, ctx) -> Result:
    """Star-schema joins, including the three role-playing date keys.

    fact_internet_sales joins dim_date three times — order, due and ship — and a
    naming convention cannot tell them apart, so each is checked separately.
    """
    joins = [
        ("fact_internet_sales", "customer_key", "dim_customer", "customer_key"),
        ("fact_internet_sales", "product_key", "dim_product", "product_key"),
        ("fact_internet_sales", "order_date_key", "dim_date", "date_key"),
        ("fact_internet_sales", "due_date_key", "dim_date", "date_key"),
        ("fact_internet_sales", "ship_date_key", "dim_date", "date_key"),
        ("fact_reseller_sales", "reseller_key", "dim_reseller", "reseller_key"),
    ]
    bad, done = [], []
    for ft, fk, dt, dk in joins:
        if ft not in lake or dt not in lake:
            continue
        dim = set(lake[dt].arrow.column(dk).to_pylist())
        orphans = {v for v in lake[ft].arrow.column(fk).to_pylist() if v not in dim}
        done.append(f"{ft}.{fk}->{dt}")
        if orphans:
            bad.append(f"{ft}.{fk} -> {dt}.{dk}: {len(orphans)} orphan key(s) "
                       f"e.g. {sorted(orphans)[:3]}")
    if bad:
        return Result(FAIL, f"{len(bad)} join(s) with orphans", bad)
    return Result(PASS, f"0 orphans across {len(done)} joins",
                  ["role-playing dates checked separately: order, due, ship"])


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
    notes = [f"not reached (reported, not ignored): {', '.join(absent)}"] if absent else []
    return Result(PASS, f"{len(agree)} engine(s) agree on {want:,} rows: " + ", ".join(agree), notes)


CHECKS: dict[str, Callable] = {
    "known_baseline": check_known_baseline,
    "row_counts": check_row_counts,
    "types": check_types,
    "nullability_flags": check_nullability_flags,
    "nullability_enforced": check_nullability_enforced,
    "null_reconciliation": check_null_reconciliation,
    "source_values": check_source_values,
    "referential_integrity": check_referential_integrity,
    "decimal_exactness": check_decimal_exactness,
    "cross_engine": check_cross_engine,
}

# Stated on every run. A suite that implies a passing run means "everything is
# verified" is worse than one that says where its edges are.
NOT_CHECKED = [
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
    catalog = RestCatalog(
        CATALOG_NAME, **{"uri": CATALOG_URI, "warehouse": WAREHOUSE, **s3_properties()}
    )
    try:
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

    ctx = {"catalog": catalog}
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
