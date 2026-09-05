"""Adapter protocol for code-to-conversation lookup evaluation.

`loci context <file>:<symbol>` ("git blame for the conversation that shaped
this code") is a distinct feature from keyword/semantic search (`loci
search`, BM25+HNSW/RRF) — different query shape, different job. This eval
harness measures the lookup feature on its own terms (does it recover the
conversations that actually touched a symbol) rather than pitting it against
search adapters that answer a different question. A separate keyword-recall
harness for the search feature is future work, not this module's concern.
"""

from __future__ import annotations

from typing import Protocol

from codeatrium.eval.datasets.schema import Query


class Adapter(Protocol):
    """`retrieve` returns exchange ids ranked best-first, truncated to `k`."""

    id: str

    def retrieve(self, query: Query, k: int) -> list[str]: ...
