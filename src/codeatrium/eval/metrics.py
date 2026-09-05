"""Retrieval-quality metrics — pure functions over ranked ids vs. a gold set.

`ranked` is an adapter's output, best match first. `gold` is the exhaustive
set of exchange ids considered correct for the query. Neither function reads
the database or knows about `Query`/`RetrievalResult` — they only compare
plain id collections, so they are trivial to unit test in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable


def recall_at_k(ranked: list[str], gold: Iterable[str], k: int) -> float:
    """Fraction of `gold` present in the top `k` of `ranked`."""
    gold_set = frozenset(gold)
    if not gold_set:
        raise ValueError("gold must be non-empty")
    if k <= 0:
        raise ValueError("k must be positive")
    top_k = frozenset(ranked[:k])
    return len(top_k & gold_set) / len(gold_set)


def mrr(ranked: list[str], gold: Iterable[str], k: int) -> float:
    """Reciprocal rank of the first gold hit within the top `k`, else 0.0."""
    gold_set = frozenset(gold)
    if not gold_set:
        raise ValueError("gold must be non-empty")
    if k <= 0:
        raise ValueError("k must be positive")
    for rank, exchange_id in enumerate(ranked[:k], start=1):
        if exchange_id in gold_set:
            return 1.0 / rank
    return 0.0
