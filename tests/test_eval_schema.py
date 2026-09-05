"""Query/RetrievalResult dataclass round-trip and validation (E1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeatrium.eval.datasets.schema import (
    Query,
    dataset_path,
    dump_dataset,
    load_dataset,
)


def test_query_round_trips_through_jsonl(tmp_path: Path) -> None:
    queries = [
        Query(id="q1", kind="symbol", value="src/foo.py::Foo.bar", gold_exchange_ids=("e1", "e2")),
        Query(id="q2", kind="text", value="how does auth work", gold_exchange_ids=("e3",)),
    ]
    path = tmp_path / "ds.v0.jsonl"
    dump_dataset(queries, path)
    loaded = load_dataset(path)
    assert loaded == queries


def test_query_rejects_empty_gold() -> None:
    with pytest.raises(ValueError, match="gold_exchange_ids"):
        Query(id="q1", kind="text", value="x", gold_exchange_ids=())


def test_query_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        Query(id="q1", kind="bogus", value="x", gold_exchange_ids=("e1",))  # type: ignore[arg-type]


def test_query_rejects_empty_id_and_value() -> None:
    with pytest.raises(ValueError, match="id"):
        Query(id="", kind="text", value="x", gold_exchange_ids=("e1",))
    with pytest.raises(ValueError, match="value"):
        Query(id="q1", kind="text", value="", gold_exchange_ids=("e1",))


def test_load_dataset_reports_offending_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.v0.jsonl"
    path.write_text('{"id": "q1", "kind": "text", "value": "x", "gold_exchange_ids": []}\n')
    with pytest.raises(ValueError, match=r"bad\.v0\.jsonl:1"):
        load_dataset(path)


def test_load_dataset_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "ds.v0.jsonl"
    path.write_text(
        '{"id": "q1", "kind": "text", "value": "x", "gold_exchange_ids": ["e1"]}\n'
        "\n"
        '{"id": "q2", "kind": "text", "value": "y", "gold_exchange_ids": ["e2"]}\n'
    )
    loaded = load_dataset(path)
    assert [q.id for q in loaded] == ["q1", "q2"]


def test_dataset_path_uses_versioned_filename() -> None:
    path = dataset_path("symbol-recall", version="v0")
    assert path.name == "symbol-recall.v0.jsonl"
