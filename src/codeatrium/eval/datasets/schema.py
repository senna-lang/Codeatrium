"""Eval dataset schema — Query / RetrievalResult + versioned JSONL (de)serialization.

Datasets are (query, gold) pairs generated once by `eval/gen/*.py`, committed to
the repo as `<name>.vN.jsonl` for reproducibility, and consumed by `eval/runner.py`.
The corpus itself (`.codeatrium/memory.db`) is never committed — only the derived
query/gold pairs are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

QueryKind = Literal["symbol", "text", "branch"]
_VALID_KINDS: frozenset[str] = frozenset({"symbol", "text", "branch"})

DATASETS_DIR = Path(__file__).resolve().parent


def dataset_path(name: str, version: str = "v0") -> Path:
    """Path to the committed JSONL for a dataset name/version."""
    return DATASETS_DIR / f"{name}.{version}.jsonl"


@dataclass(frozen=True)
class Query:
    """One retrieval query plus its exhaustive gold answer set.

    `value` holds the query payload; its meaning depends on `kind`:
      - "text": a free-text question.
      - "symbol": `"<repo-relative-file-path>::<symbol-name>"` — carries the
        file grounding a symbol-aware adapter can exploit; adapters without
        file grounding (grep/bm25/rrf) use only the trailing symbol name.
      - "branch": a git branch name.
    """

    id: str
    kind: QueryKind
    value: str
    gold_exchange_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Query.id must be non-empty")
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"Query.kind must be one of {sorted(_VALID_KINDS)}, got {self.kind!r}"
            )
        if not self.value:
            raise ValueError("Query.value must be non-empty")
        if not self.gold_exchange_ids:
            raise ValueError("Query.gold_exchange_ids must be non-empty")


@dataclass(frozen=True)
class RetrievalResult:
    """One adapter's ranked output for one query (best-first)."""

    query_id: str
    ranked_exchange_ids: tuple[str, ...]


def dump_dataset(queries: list[Query], path: Path) -> None:
    """Write `queries` as newline-delimited JSON, one record per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for query in queries:
            record = {
                "id": query.id,
                "kind": query.kind,
                "value": query.value,
                "gold_exchange_ids": list(query.gold_exchange_ids),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_dataset(path: Path) -> list[Query]:
    """Read a JSONL dataset back into `Query` records.

    Raises `ValueError` naming the offending line on any malformed record
    (missing field, empty gold set, unknown kind) instead of silently
    skipping it — a corrupted dataset should fail loudly, not shrink quietly.
    """
    queries: list[Query] = []
    with path.open(encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            row = json.loads(line)
            try:
                queries.append(
                    Query(
                        id=row["id"],
                        kind=row["kind"],
                        value=row["value"],
                        gold_exchange_ids=tuple(row["gold_exchange_ids"]),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{path}:{line_no}: invalid Query record: {exc}") from exc
    return queries
