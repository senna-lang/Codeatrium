"""Run every adapter against every query in a dataset (E5).

`run_adapters` is deliberately dumb — a total cross-product with no scoring —
so adapter execution stays decoupled from metric computation (`report.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from codeatrium.eval.adapters.base import Adapter
from codeatrium.eval.datasets.schema import Query, RetrievalResult


@dataclass(frozen=True)
class CorpusStats:
    """Corpus size recorded alongside every report for reproducibility."""

    exchange_count: int
    session_count: int
    query_count: int


@dataclass(frozen=True)
class AdapterRunResult:
    adapter_id: str
    results: tuple[RetrievalResult, ...]


def corpus_stats(db_path: Path, queries: list[Query]) -> CorpusStats:
    from codeatrium.db import get_connection

    con = get_connection(db_path)
    try:
        exchange_count = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
        session_count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        con.close()
    return CorpusStats(
        exchange_count=exchange_count,
        session_count=session_count,
        query_count=len(queries),
    )


def run_adapters(
    adapters: Sequence[Adapter], queries: list[Query], k: int
) -> list[AdapterRunResult]:
    """adapter × query total cross-product."""
    runs: list[AdapterRunResult] = []
    for adapter in adapters:
        results = tuple(
            RetrievalResult(
                query_id=query.id,
                ranked_exchange_ids=tuple(adapter.retrieve(query, k)),
            )
            for query in queries
        )
        runs.append(AdapterRunResult(adapter_id=adapter.id, results=results))
    return runs
