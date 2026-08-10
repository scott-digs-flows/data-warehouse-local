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

from _common import REPO_ROOT

MANIFEST_PATH = REPO_ROOT / "engines.yaml"


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

    def __getitem__(self, name: str) -> Engine:
        for e in self.engines:
            if e.name == name:
                return e
        raise KeyError(f"no engine named {name!r} in {MANIFEST_PATH.name}")

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.engines]

    def by_tier(self, tier: str) -> list[Engine]:
        return [e for e in self.engines if e.tier == tier]


def load(path: Path = MANIFEST_PATH) -> Manifest:
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
    return Manifest(version=raw["version"], dataset=raw["dataset"], engines=engines)
