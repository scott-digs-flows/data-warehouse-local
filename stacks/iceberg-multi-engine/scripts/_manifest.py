"""Reader for engines.yaml — the contract between this repo and the BI app.

Kept deliberately small: the manifest is data, and consumers (including the BI
app, which will reimplement this in its own codebase) should not need much
machinery to use it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from _common import STACK_ROOT

# engines.yaml is stack-local: each stack owns its own manifest, the way it
# owns its own .env and compose project name. This resolved from the repo
# root instead -- one level too high -- so no manifest was ever found and
# smoke_test.py could not run at all. Only shared/ artifacts (the raw data
# and the pinned schemas) belong to the repo root.
MANIFEST_PATH = STACK_ROOT / "engines.yaml"


@dataclass
class Engine:
    name: str
    tier: str
    dialect: str
    driver: str
    dsn: str | None
    connection: dict[str, Any] = field(default_factory=dict)
    qualify: str = '"{table}"'
    identifier_case: str = "lower"
    sync: dict[str, Any] | None = None
    notes: str = ""

    @property
    def is_iceberg_native(self) -> bool:
        return self.tier == "iceberg-native"

    def table_ref(self, table: str) -> str:
        """Fully-qualified, engine-specific reference for a table name.

        Engines disagree on how the Iceberg namespace surfaces — Trino nests it
        as a schema, ClickHouse folds it into the table name — so the manifest
        carries a template rather than the app hardcoding the difference.
        """
        return self.qualify.format(table=table)


@dataclass
class Manifest:
    version: int
    dataset: dict[str, Any]
    engines: list[Engine]
    # Which file this came from, so errors name the manifest actually read
    # rather than whatever the module-level default happened to be.
    path: Path | None = None

    def __getitem__(self, name: str) -> Engine:
        for e in self.engines:
            if e.name == name:
                return e
        raise KeyError(
            f"no engine named {name!r} in {(self.path or MANIFEST_PATH).name}. "
            f"Known: {', '.join(self.names)}"
        )

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.engines]

    def by_tier(self, tier: str) -> list[Engine]:
        return [e for e in self.engines if e.tier == tier]


def load(path: Path | None = None) -> Manifest:
    """Read the manifest. Defaults to this stack's engines.yaml.

    The default is resolved here rather than bound in the signature: a default
    argument is evaluated once, at import time, so `path=MANIFEST_PATH` would
    freeze whatever the module-level value was then and silently ignore any
    later reassignment. This file is the reference implementation the BI app
    reimplements in its own codebase, so the shape is worth getting right.
    """
    path = path or MANIFEST_PATH
    raw = yaml.safe_load(path.read_text())
    engines = [
        Engine(
            name=e["name"],
            tier=e["tier"],
            dialect=e["dialect"],
            driver=e["driver"],
            dsn=e.get("dsn"),
            connection=e.get("connection") or {},
            qualify=e.get("qualify", '"{table}"'),
            identifier_case=e.get("identifier_case", "lower"),
            sync=e.get("sync"),
            notes=(e.get("notes") or "").strip(),
        )
        for e in raw["engines"]
    ]
    return Manifest(version=raw["version"], dataset=raw["dataset"], engines=engines,
                    path=path)
