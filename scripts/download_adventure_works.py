"""Download AdventureWorksDW and export every user table to CSV.

Pipeline:
  1. Download AdventureWorksDW2022.bak from Microsoft's sql-server-samples release.
  2. Spin up a transient SQL Server container.
  3. Restore the .bak into it.
  4. bcp each user table out (pipe-delimited intermediate).
  5. Convert each to proper CSV (comma-separated, quoted) under data/adventure_works_dw/.
  6. Stop and remove the container.

Requires `docker` on PATH. On Apple Silicon SQL Server runs under amd64 emulation.
"""

from __future__ import annotations

import csv
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
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

console = Console()

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=False)

BAK_URL = os.environ.get(
    "AW_BAK_URL",
    "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorksDW2022.bak",
)
DATA_DIR = REPO_ROOT / os.environ.get("AW_DATA_DIR", "data/adventure_works_dw").lstrip("./")
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


def in_container_sqlcmd(query: str) -> str:
    r = sh(
        [
            "docker", "exec", CONTAINER_NAME,
            "/opt/mssql-tools18/bin/sqlcmd",
            "-S", "localhost", "-U", "sa", "-P", SA_PASSWORD,
            "-C", "-h", "-1", "-W", "-s", "|",
            "-Q", query,
        ],
        capture_output=True,
    )
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
        f"USE [{DB_NAME}]; "
        "SET NOCOUNT ON; "
        "SELECT TABLE_SCHEMA + '.' + TABLE_NAME "
        "FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' "
        "AND TABLE_NAME NOT LIKE 'sys%' "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME"
    )
    tables: list[TableRef] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "." not in line or line.startswith("-"):
            continue
        schema, name = line.split(".", 1)
        tables.append(TableRef(schema=schema, name=name))
    console.print(f"[green]✓[/green] Found {len(tables)} tables")
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
            "-C", "-c", "-t|", "-r\n", "-k",
        ],
        capture_output=True,
    )
    host_path = RAW_DIR / f"{table.name}.pipe"
    sh(["docker", "cp", f"{CONTAINER_NAME}:{container_path}", str(host_path)])
    sh(["docker", "exec", CONTAINER_NAME, "rm", "-f", container_path])
    return host_path


def column_names(table: TableRef) -> list[str]:
    out = in_container_sqlcmd(
        f"USE [{DB_NAME}]; SET NOCOUNT ON; "
        f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{table.schema}' AND TABLE_NAME = '{table.name}' "
        f"ORDER BY ORDINAL_POSITION"
    )
    cols = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("-")]
    return cols


def pipe_to_csv(table: TableRef, pipe_path: Path) -> Path:
    """Convert pipe-delimited bcp output to proper CSV with header + quoting."""
    cols = column_names(table)
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
            # bcp -c writes NULL as empty string; that's already the right CSV behavior.
            writer.writerow(row)
    return csv_path


def main() -> None:
    download_bak()
    with sqlserver_container():
        restore_database()
        tables = list_tables()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        for i, table in enumerate(tables, 1):
            console.print(f"[cyan]({i}/{len(tables)}) exporting[/cyan] {table.fq}")
            pipe_path = bcp_table(table)
            csv_path = pipe_to_csv(table, pipe_path)
            rows = sum(1 for _ in open(csv_path, encoding="utf-8")) - 1
            console.print(f"  → {csv_path.name}  ({rows:,} rows)")
    # Cleanup pipe intermediates.
    if RAW_DIR.exists():
        shutil.rmtree(RAW_DIR)
    console.print(f"[bold green]Done.[/bold green] CSVs in {DATA_DIR}")


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
