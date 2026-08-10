"""Shared result types for loaders + benchmark."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class TableResult:
    loader: str
    table: str
    rows: int
    seconds: float


def total_seconds(results: list[TableResult]) -> float:
    return sum(r.seconds for r in results)


def total_rows(results: list[TableResult]) -> int:
    return sum(r.rows for r in results)


def to_dicts(results: list[TableResult]) -> list[dict]:
    return [asdict(r) for r in results]
