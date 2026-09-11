"""Download AdventureWorksDW and export every user table to CSV + schema JSON.

Pipeline:
  1. Download AdventureWorksDW2022.bak from Microsoft's sql-server-samples release.
  2. Spin up a transient SQL Server container.
  3. Restore the .bak into it.
  4. bcp each user table out (pipe-delimited intermediate).
  5. Convert each to proper CSV (comma-separated, quoted) under data/adventure_works_dw/.
  6. Write authoritative column types to schemas/<Table>.json.
  7. Stop and remove the container.

The CSV extract stays faithful to the source: original PascalCase identifiers,
no renaming. Normalisation to snake_case happens at load time in load_iceberg.py.

The schema JSON exists so loaders never infer types from CSV text. Inference
disagrees across readers (int32 vs int64, string vs date, decimal handling),
which would silently make each engine's view of the data subtly different.

Requires `docker` on PATH. On Apple Silicon SQL Server runs under amd64 emulation.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from common import EXCLUDED_TABLES, SCHEMA_DIR, snake_case
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

console = Console()

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env", override=False)

BAK_URL = os.environ.get(
    "AW_BAK_URL",
    "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorksDW2022.bak",
)
DATA_DIR = REPO_ROOT / os.environ.get("AW_DATA_DIR", "shared/data/adventure_works_dw").lstrip("./")
BAK_PATH = DATA_DIR / "AdventureWorksDW2022.bak"
RAW_DIR = DATA_DIR / "_raw_pipe"
SA_PASSWORD = os.environ.get("MSSQL_SA_PASSWORD", "Aw_DW_local_123!")
CONTAINER_NAME = "aw-dw-extract"
MSSQL_IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
DB_NAME = "AdventureWorksDW2022"


@dataclass
class TableRef:
    schema: str
    name: str

    @property
    def fq(self) -> str:
        return f"[{self.schema}].[{self.name}]"

    @property
    def filename(self) -> str:
        return f"{self.name}.csv"


def sh(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, surfacing stderr on failure."""
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def download_bak() -> None:
    if BAK_PATH.exists() and BAK_PATH.stat().st_size > 10_000_000:
        console.print(f"[green]✓[/green] .bak already present: {BAK_PATH}")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    console.print(f"[cyan]Downloading[/cyan] {BAK_URL}")
    with urllib.request.urlopen(BAK_URL) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task("AdventureWorksDW2022.bak", total=total or None)
            with open(BAK_PATH, "wb") as fh:
                while chunk := resp.read(1024 * 256):
                    fh.write(chunk)
                    progress.update(task, advance=len(chunk))
    console.print(f"[green]✓[/green] saved {BAK_PATH}")


@contextmanager
def sqlserver_container():
    """Start a SQL Server container, yield, then tear down."""
    # Remove any stale container with the same name.
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)

    console.print(f"[cyan]Starting[/cyan] {MSSQL_IMAGE} ({CONTAINER_NAME})")
    sh(
        [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "--platform", "linux/amd64",
            "-e", "ACCEPT_EULA=Y",
            "-e", f"MSSQL_SA_PASSWORD={SA_PASSWORD}",
            "-e", "MSSQL_PID=Developer",
            "-p", "11433:1433",
            MSSQL_IMAGE,
        ]
    )
    try:
        wait_for_sqlserver()
        yield
    finally:
        console.print(f"[dim]Removing container {CONTAINER_NAME}[/dim]")
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)


def wait_for_sqlserver(timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = subprocess.run(
            [
                "docker", "exec", CONTAINER_NAME,
                "/opt/mssql-tools18/bin/sqlcmd",
                "-S", "localhost", "-U", "sa", "-P", SA_PASSWORD,
                "-C", "-Q", "SELECT 1",
            ],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            console.print("[green]✓[/green] SQL Server is up")
            return
        time.sleep(2)
    raise RuntimeError("SQL Server did not become ready in time")


def in_container_sqlcmd(query: str, db: str | None = None) -> str:
    cmd = [
        "docker", "exec", CONTAINER_NAME,
        "/opt/mssql-tools18/bin/sqlcmd",
        "-S", "localhost", "-U", "sa", "-P", SA_PASSWORD,
        "-C", "-h", "-1", "-W", "-s", "|",
    ]
    if db:
        # Use -d instead of `USE [db]` so SQL Server doesn't emit the
        # "Changed database context to ..." line that pollutes parsed output.
        cmd += ["-d", db]
    cmd += ["-Q", query]
    r = sh(cmd, capture_output=True)
    return r.stdout


def restore_database() -> None:
    console.print(f"[cyan]Copying .bak into container[/cyan]")
    sh(["docker", "exec", CONTAINER_NAME, "mkdir", "-p", "/var/opt/mssql/backup"])
    sh(["docker", "cp", str(BAK_PATH), f"{CONTAINER_NAME}:/var/opt/mssql/backup/AdventureWorksDW2022.bak"])

    console.print(f"[cyan]Inspecting logical file names[/cyan]")
    filelist = in_container_sqlcmd(
        "RESTORE FILELISTONLY FROM DISK = '/var/opt/mssql/backup/AdventureWorksDW2022.bak'"
    )
    # Parse first column (LogicalName) from sqlcmd output.
    logical_data = None
    logical_log = None
    for line in filelist.splitlines():
        line = line.strip()
        if not line or line.startswith("-"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, _, kind = parts[0], parts[1], parts[2]
        if kind == "D" and logical_data is None:
            logical_data = name
        elif kind == "L" and logical_log is None:
            logical_log = name
    if not logical_data or not logical_log:
        console.print(f"[red]Could not parse FILELIST output:[/red]\n{filelist}")
        raise RuntimeError("Failed to parse RESTORE FILELISTONLY output")
    console.print(f"  data file:  [yellow]{logical_data}[/yellow]")
    console.print(f"  log file:   [yellow]{logical_log}[/yellow]")

    console.print(f"[cyan]Restoring database {DB_NAME}[/cyan]")
    restore_sql = (
        f"RESTORE DATABASE [{DB_NAME}] "
        f"FROM DISK = '/var/opt/mssql/backup/AdventureWorksDW2022.bak' "
        f"WITH MOVE '{logical_data}' TO '/var/opt/mssql/data/{DB_NAME}.mdf', "
        f"MOVE '{logical_log}' TO '/var/opt/mssql/data/{DB_NAME}_log.ldf', "
        f"REPLACE, RECOVERY"
    )
    in_container_sqlcmd(restore_sql)
    console.print(f"[green]✓[/green] Database restored")


def list_tables() -> list[TableRef]:
    out = in_container_sqlcmd(
        "SET NOCOUNT ON; "
        "SELECT TABLE_SCHEMA + '.' + TABLE_NAME "
        "FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' "
        "AND TABLE_NAME NOT LIKE 'sys%' "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME",
        db=DB_NAME,
    )
    tables: list[TableRef] = []
    skipped: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "." not in line or line.startswith("-"):
            continue
        schema, name = line.split(".", 1)
        if name in EXCLUDED_TABLES:
            skipped.append(name)
            continue
        tables.append(TableRef(schema=schema, name=name))
    console.print(f"[green]✓[/green] Found {len(tables)} tables")
    if skipped:
        console.print(f"[dim]  excluded: {', '.join(skipped)}[/dim]")
    return tables


def bcp_table(table: TableRef) -> Path:
    """Export one table to /tmp inside the container, then copy to host."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    container_path = f"/tmp/{table.name}.pipe"
    sh(
        [
            "docker", "exec", CONTAINER_NAME,
            "/opt/mssql-tools18/bin/bcp",
            f"[{DB_NAME}].{table.fq}", "out", container_path,
            "-S", "localhost", "-U", "sa", "-P", SA_PASSWORD,
            # -u = trust server certificate (bcp 18 equivalent of sqlcmd's -C).
            # -c = character format, -t = field terminator, -r = row terminator,
            # -k = keep NULLs.
            "-u", "-c", "-t|", "-r\n", "-k",
        ],
        capture_output=True,
    )
    host_path = RAW_DIR / f"{table.name}.pipe"
    sh(["docker", "cp", f"{CONTAINER_NAME}:{container_path}", str(host_path)])
    sh(["docker", "exec", CONTAINER_NAME, "rm", "-f", container_path])
    return host_path


def _nullable(v: str) -> int | None:
    return None if v.upper() == "NULL" or not v else int(v)


def table_columns(table: TableRef) -> list[dict]:
    """Authoritative column metadata straight from INFORMATION_SCHEMA.

    Carries both the source identifier and its snake_case form so the loader
    never has to re-derive the mapping, and so the transformation is auditable
    in a checked-in artifact.
    """
    out = in_container_sqlcmd(
        "SET NOCOUNT ON; "
        "SELECT COLUMN_NAME, DATA_TYPE, "
        "ISNULL(CAST(CHARACTER_MAXIMUM_LENGTH AS VARCHAR), 'NULL'), "
        "ISNULL(CAST(NUMERIC_PRECISION AS VARCHAR), 'NULL'), "
        "ISNULL(CAST(NUMERIC_SCALE AS VARCHAR), 'NULL'), "
        "IS_NULLABLE "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{table.schema}' AND TABLE_NAME = '{table.name}' "
        "ORDER BY ORDINAL_POSITION",
        db=DB_NAME,
    )
    cols: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
        name, sql_type, char_len, precision, scale, is_nullable = parts[:6]
        cols.append({
            "source_name": name,
            "name": snake_case(name),
            "sql_type": sql_type.lower(),
            "max_length": _nullable(char_len),
            "precision": _nullable(precision),
            "scale": _nullable(scale),
            "nullable": is_nullable.upper() == "YES",
        })
    return cols


FK_PATH = SCHEMA_DIR.parent / "foreign_keys.json"


def foreign_keys() -> list[dict]:
    """Authoritative foreign keys from sys.foreign_keys.

    The pinned schemas carry types; this carries relationships. Both are
    extracted from the source rather than inferred, for the same reason:
    inference disagrees with itself. A naming convention resolves most of this
    star schema, but provably not all of it — `fact_internet_sales_reason` joins
    `fact_internet_sales` on a two-column composite, and
    `new_fact_currency_rate.currency_id` references a natural key rather than a
    surrogate. Neither shape is expressible as `<stem>_key -> dim_<stem>`.

    Grouped by constraint, so a composite arrives as a column *list* rather than
    being flattened into unrelated single-column edges.
    """
    out = in_container_sqlcmd(
        "SET NOCOUNT ON; "
        "SELECT fk.name, ps.name, pt.name, pc.name, rs.name, rt.name, rc.name, "
        "  fkc.constraint_column_id "
        "FROM sys.foreign_keys fk "
        "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
        "JOIN sys.tables  pt ON pt.object_id = fk.parent_object_id "
        "JOIN sys.schemas ps ON ps.schema_id = pt.schema_id "
        "JOIN sys.columns pc ON pc.object_id = pt.object_id AND pc.column_id = fkc.parent_column_id "
        "JOIN sys.tables  rt ON rt.object_id = fk.referenced_object_id "
        "JOIN sys.schemas rs ON rs.schema_id = rt.schema_id "
        "JOIN sys.columns rc ON rc.object_id = rt.object_id AND rc.column_id = fkc.referenced_column_id "
        "ORDER BY fk.name, fkc.constraint_column_id",
        db=DB_NAME,
    )
    grouped: dict[str, dict] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue
        name, pschema, ptable, pcol, rschema, rtable, rcol, _ord = parts[:8]
        # A constraint touching an excluded table cannot be checked downstream,
        # so recording it would promise coverage the lake cannot deliver.
        if ptable in EXCLUDED_TABLES or rtable in EXCLUDED_TABLES:
            continue
        fk = grouped.setdefault(name, {
            "constraint": name,
            "source_from_table": f"{pschema}.{ptable}",
            "from_table": snake_case(ptable),
            "from_columns": [],
            "source_to_table": f"{rschema}.{rtable}",
            "to_table": snake_case(rtable),
            "to_columns": [],
        })
        fk["from_columns"].append(snake_case(pcol))
        fk["to_columns"].append(snake_case(rcol))
    # Sorted so two extracts are byte-identical; sqlcmd row order is not a contract.
    return sorted(grouped.values(),
                  key=lambda f: (f["from_table"], f["from_columns"], f["to_table"]))


def write_foreign_keys(fks: list[dict]) -> Path:
    """Persist the FK set as a checked-in artifact, like the pinned schemas."""
    FK_PATH.parent.mkdir(parents=True, exist_ok=True)
    FK_PATH.write_text(json.dumps({
        "source_database": DB_NAME,
        "note": "Authoritative foreign keys from sys.foreign_keys. Relationships are "
                "extracted, not inferred from naming — see verify_lake.py's "
                "foreign_key_oracle check.",
        "foreign_keys": fks,
    }, indent=2) + "\n")
    return FK_PATH


def write_schema(table: TableRef, columns: list[dict]) -> Path:
    """Persist column metadata to schemas/<Table>.json for the loader to pin."""
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    path = SCHEMA_DIR / f"{table.name}.json"
    path.write_text(json.dumps({
        "source_table": f"{table.schema}.{table.name}",
        "table": snake_case(table.name),
        "columns": columns,
    }, indent=2) + "\n")
    return path


def pipe_to_csv(table: TableRef, columns: list[dict], pipe_path: Path) -> Path:
    """Convert pipe-delimited bcp output to proper CSV with header + quoting."""
    cols = [c["source_name"] for c in columns]
    csv_path = DATA_DIR / table.filename
    with open(pipe_path, "r", encoding="utf-8", errors="replace", newline="") as src, \
            open(csv_path, "w", encoding="utf-8", newline="") as dst:
        writer = csv.writer(dst, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(cols)
        for line in src:
            line = line.rstrip("\n")
            if not line:
                continue
            row = line.split("|")
            # bcp -c writes NULL as an unquoted empty field — but it writes an empty
            # string exactly the same way, so the two are indistinguishable from here
            # on. Measured across the lake: 146 string columns, 0 empty strings,
            # 192,916 NULLs. Whether that is acceptable is DW-19.
            writer.writerow(row)
    return csv_path


def verify_csv(csv_path: Path, expected_cols: int) -> tuple[int, int]:
    """Return (rows, ragged_rows) by actually parsing the CSV.

    Counting raw lines would overreport any table whose text columns contain
    embedded newlines, and would hide the fact that bcp's unquoted character
    format had split records mid-row.
    """
    rows = ragged = 0
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            rows += 1
            if len(row) != expected_cols:
                ragged += 1
    return rows, ragged


def main() -> None:
    download_bak()
    problems: list[str] = []
    with sqlserver_container():
        restore_database()
        tables = list_tables()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        for i, table in enumerate(tables, 1):
            console.print(f"[cyan]({i}/{len(tables)}) exporting[/cyan] {table.fq}")
            columns = table_columns(table)
            write_schema(table, columns)
            pipe_path = bcp_table(table)
            csv_path = pipe_to_csv(table, columns, pipe_path)
            rows, ragged = verify_csv(csv_path, len(columns))
            if ragged:
                problems.append(f"{table.name}: {ragged:,} of {rows:,} rows malformed")
                console.print(
                    f"  → {csv_path.name}  [red]{rows:,} rows, {ragged:,} malformed[/red]"
                )
            else:
                console.print(f"  → {csv_path.name}  ({rows:,} rows)")
        fks = foreign_keys()
        write_foreign_keys(fks)
        console.print(f"[cyan]foreign keys[/cyan] {len(fks)} constraints -> {FK_PATH.name}")

    # Cleanup pipe intermediates.
    if RAW_DIR.exists():
        shutil.rmtree(RAW_DIR)

    console.print(f"[bold green]Done.[/bold green] CSVs in {DATA_DIR}, schemas in {SCHEMA_DIR}")
    if problems:
        console.print(
            "\n[bold red]Malformed extracts[/bold red] — bcp character format cannot "
            "round-trip embedded newlines. Exclude these tables or export them differently:"
        )
        for p in problems:
            console.print(f"  [red]•[/red] {p}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Command failed:[/red] {' '.join(e.cmd)}")
        if e.stdout:
            console.print(f"[red]stdout:[/red]\n{e.stdout}")
        if e.stderr:
            console.print(f"[red]stderr:[/red]\n{e.stderr}")
        sys.exit(1)
