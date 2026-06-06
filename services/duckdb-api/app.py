"""FastAPI wrapper exposing DuckDB attached to the Lakekeeper Iceberg catalog.

Endpoints:
  GET  /health
  GET  /schemas
  GET  /tables[?schema=...]
  POST /query   {"sql": "...", "params": [...], "limit": 1000}

The DuckDB connection is shared with a lock; concurrent reads are serialized.
That is fine for a local experimentation service.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

CATALOG_ALIAS = "ice"
state: dict[str, Any] = {"con": None, "lock": threading.Lock()}


def _s3_endpoint_parts(endpoint: str) -> tuple[str, str]:
    host = endpoint.replace("https://", "").replace("http://", "")
    use_ssl = "true" if endpoint.startswith("https://") else "false"
    return host, use_ssl


def setup_duckdb() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL iceberg")
    con.execute("LOAD iceberg")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")

    s3_host, use_ssl = _s3_endpoint_parts(os.environ["S3_ENDPOINT"])
    con.execute(
        f"""
        CREATE OR REPLACE SECRET minio_secret (
            TYPE s3,
            KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{s3_host}',
            REGION '{os.environ.get("AWS_REGION", "us-east-1")}',
            URL_STYLE 'path',
            USE_SSL {use_ssl}
        )
        """
    )
    con.execute(
        f"""
        ATTACH '{os.environ["ICEBERG_WAREHOUSE"]}' AS {CATALOG_ALIAS} (
            TYPE iceberg,
            ENDPOINT '{os.environ["ICEBERG_CATALOG_URI"]}'
        )
        """
    )
    return con


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state["con"] = setup_duckdb()
    yield
    state["con"].close()


app = FastAPI(title="DuckDB Iceberg API", lifespan=lifespan)


class Query(BaseModel):
    sql: str
    params: list[Any] | None = None
    limit: int | None = 1000


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


@app.get("/health")
def health() -> dict[str, str]:
    with state["lock"]:
        state["con"].execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/schemas")
def list_schemas() -> dict[str, list[str]]:
    with state["lock"]:
        rows = state["con"].execute(
            "SELECT DISTINCT schema_name FROM duckdb_tables() "
            f"WHERE database_name = '{CATALOG_ALIAS}' ORDER BY schema_name"
        ).fetchall()
    return {"schemas": [r[0] for r in rows]}


@app.get("/tables")
def list_tables(schema: str | None = None) -> dict[str, list[dict[str, str]]]:
    with state["lock"]:
        if schema:
            rows = state["con"].execute(
                "SELECT schema_name, table_name FROM duckdb_tables() "
                f"WHERE database_name='{CATALOG_ALIAS}' AND schema_name=? "
                "ORDER BY table_name",
                [schema],
            ).fetchall()
        else:
            rows = state["con"].execute(
                "SELECT schema_name, table_name FROM duckdb_tables() "
                f"WHERE database_name='{CATALOG_ALIAS}' "
                "ORDER BY schema_name, table_name"
            ).fetchall()
    return {"tables": [{"schema": s, "name": n} for s, n in rows]}


@app.post("/query")
def query(q: Query) -> dict[str, Any]:
    try:
        with state["lock"]:
            cur = state["con"].execute(q.sql, q.params or [])
            cols = [d[0] for d in cur.description] if cur.description else []
            raw = cur.fetchmany(q.limit) if q.limit else cur.fetchall()
    except duckdb.Error as e:
        raise HTTPException(status_code=400, detail=str(e))
    rows = [[_jsonable(v) for v in r] for r in raw]
    return {"columns": cols, "rows": rows, "row_count": len(rows)}
