---
name: stack-operations
description: Running and troubleshooting the local Docker stacks — startup order, compose profiles, required /etc/hosts entries, ports, credentials, teardown, and the environment failure modes that have already cost debugging time. Use when bringing a stack up or down, or when something is not reachable, not healthy, or not starting.
---

# Running the stacks

Everything runs locally in Docker Compose. Commands run **from inside the stack
directory**, which owns its own `.env` and compose project name.

## Required `/etc/hosts` entries

```
127.0.0.1  lakekeeper
127.0.0.1  minio
```

**This is the single most likely reason a fresh checkout does not work.** The
Iceberg REST catalog advertises its own URI and object-storage endpoint using
*internal Docker hostnames* (`http://lakekeeper:8181`, `http://minio:9000`).
Host-side scripts follow those names, so without the entries every load and
query fails in confusing, unrelated-looking ways.

Check before debugging anything else:

```bash
getent hosts lakekeeper minio || grep -E 'lakekeeper|minio' /etc/hosts
```

## Bringing the lakehouse up

```bash
cd stacks/iceberg-multi-engine
cp .env.example .env          # single source for credentials and ports
uv sync --extra duckdb-api

docker compose up -d                                # lake: MinIO + Postgres + Lakekeeper
uv run python scripts/load_iceberg.py               # raw → Iceberg
docker compose --profile clickhouse up -d           # the warehouse engine
uv run python scripts/create_clickhouse_views.py    # make tables introspectable
uv run python scripts/smoke_test.py --engine clickhouse
```

Lake services carry **no profile** and always start. Engines sit behind
profiles, so you only pay for what you use:

```bash
docker compose --profile clickhouse up -d   # lake + ClickHouse   (4 containers, ~1.0 GB)
docker compose --profile trino up -d        # lake + Trino
docker compose --profile duckdb up -d       # lake + DuckDB HTTP
docker compose --profile engines up -d      # lake + all three    (6 containers, ~2.1 GB)
```

One-time, ~10 minutes, before any of this: `uv run python
shared/scripts/extract_source.py` restores the `.bak` into SQL Server (under
amd64 emulation on Apple Silicon) and exports CSVs plus pinned schemas.

## Ports

| Service | Port | Note |
| --- | --- | --- |
| MinIO API | 9000 | Owns `9000`, which is why ClickHouse native moved |
| MinIO console | 9001 | |
| Lakekeeper | 8181 | REST catalog, `/catalog` path |
| Trino | 8080 | |
| DuckDB HTTP | 8000 | Interactive docs at `/docs` |
| ClickHouse HTTP | 8123 | |
| ClickHouse native | 9010 | Moved off 9000 to avoid MinIO |
| ClickHouse pg-wire | 9005 | **The reliable route for GUI clients** |
| Postgres | 5432 | |

Credentials live in the stack's `.env`, seeded from `.env.example`. Defaults:
MinIO `admin` / `admin12345`, ClickHouse `default` / `clickhouse`.

## Failure modes

**`network … not found` on `up`.** Containers store the network *ID* they were
created with. If `warehouse-net` was recreated since — a `down` and later `up`,
or a Docker restart — older containers still point at the dead ID, and Compose
tries to *start* them rather than recreate them.

```bash
docker rm -f trino duckdb-api clickhouse clickhouse-init
docker compose --profile engines up -d
```

Volumes are untouched, so the lake survives. `docker compose --profile all down`
before bringing the stack back up avoids it entirely.

**Container healthy but unreachable from the host.** ClickHouse ships listening
on `::1` only, and its healthcheck runs *inside* the container — so it reports
healthy while nothing can reach it. `config.d/network.xml` fixes it. Check this
before suspecting the network.

**Catalog permanently unattached, no obvious symptom.** `initdb.d` scripts run
only on a first-ever start, so one failed boot leaves ClickHouse without the
catalog forever. Attachment is an idempotent one-shot compose service for this
reason — keep it that way rather than reverting to an init script.

**MinIO 403s on the first few operations of a process**, then settles.
Reproduced under both s3fs and PyArrowFileIO and two MinIO releases — not a
client bug. `with_s3_retry()` in `_common.py` absorbs it. Only wrap idempotent
operations.

**DuckDB 403s on manifest reads while plain `s3://` works.** DuckDB secrets are
scoped to `s3://`, but the Iceberg extension fetches manifest `.avro` files over
an `http://` URL, which falls outside a `CREATE SECRET` scope — so those go out
unsigned. Use the global `SET s3_*` settings instead (see `attach_sql` in
`engines.yaml`).

**JSON over HTTP loses numeric types.** The DuckDB HTTP endpoint returns
`SUM(...)` as the string `"3649866.5512"`. Not a bug to fix here — but a real
constraint for any consumer on a JSON transport, which has to re-infer types a
native driver would have preserved.

## Diagnosing, in order

```bash
docker compose ps                     # what is actually up, and healthy
docker compose logs --tail=50 <svc>   # what it said on the way down
docker stats --no-stream              # memory pressure
curl -sS --max-time 3 http://localhost:8181/health     # catalog alive?
curl -sS --max-time 3 "http://localhost:8123/ping"     # ClickHouse alive?
```

Then, in order: `/etc/hosts` entries → container health → is it listening on the
right interface → is the catalog attached → are credentials right.

## Teardown

```bash
docker compose --profile all down          # stop, keep data
docker compose --profile all down -v       # also delete volumes — destroys the lake
```

`down -v` means a full re-extract is **not** needed (CSVs live in `shared/` and
survive), but every Iceberg table must be reloaded. Stopped stacks cost nothing,
so prefer `down` over `down -v`.

## Only one stack

`stacks/iceberg-multi-engine/` is the only stack. The off-pipeline
`stacks/clickhouse/` was removed under DW-15, so there is nothing to run
alongside and nothing to collide with. Engines within the stack sit behind
compose profiles — that is the knob for paying only for what you use.
