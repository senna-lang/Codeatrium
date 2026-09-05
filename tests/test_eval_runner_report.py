"""Deterministic completeness report from fake adapter runs — no baseline column (E5)."""

from __future__ import annotations

import json
from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.eval.datasets.schema import Query, RetrievalResult
from codeatrium.eval.report import render_json, render_markdown, score_runs
from codeatrium.eval.runner import (
    AdapterRunResult,
    CorpusStats,
    corpus_stats,
    run_adapters,
)


class _FakeAdapter:
    def __init__(self, id_: str, answers: dict[str, list[str]]) -> None:
        self.id = id_
        self._answers = answers

    def retrieve(self, query: Query, k: int) -> list[str]:
        return self._answers.get(query.id, [])[:k]


def _queries() -> list[Query]:
    return [
        Query(id="q1", kind="symbol", value="src/foo.py::list_dir", gold_exchange_ids=("e1",)),
        Query(
            id="q2",
            kind="symbol",
            value="src/bar.py::helper",
            gold_exchange_ids=("e2", "e3"),
        ),
    ]


def test_run_adapters_is_a_total_cross_product() -> None:
    queries = _queries()
    symbol = _FakeAdapter("symbol", {"q1": ["e1"], "q2": ["e2", "e3"]})

    runs = run_adapters([symbol], queries, k=10)

    assert [r.adapter_id for r in runs] == ["symbol"]
    assert runs[0].results == (
        RetrievalResult(query_id="q1", ranked_exchange_ids=("e1",)),
        RetrievalResult(query_id="q2", ranked_exchange_ids=("e2", "e3")),
    )


def test_score_runs_is_a_standalone_completeness_metric() -> None:
    queries = _queries()
    runs = [
        AdapterRunResult(
            adapter_id="symbol",
            results=(
                RetrievalResult(query_id="q1", ranked_exchange_ids=("e1",)),
                RetrievalResult(query_id="q2", ranked_exchange_ids=("e2",)),
            ),
        ),
    ]

    scores = score_runs(runs, queries)

    assert len(scores) == 1
    assert scores[0].adapter_id == "symbol"
    assert scores[0].mrr_at_10 == 1.0
    assert scores[0].recall_at[10] == 0.75  # (1/1 + 1/2) / 2
    assert not hasattr(scores[0], "baseline_mrr_delta")


def test_score_runs_zero_hits_is_zero() -> None:
    queries = _queries()
    runs = [
        AdapterRunResult(
            adapter_id="symbol",
            results=(
                RetrievalResult(query_id="q1", ranked_exchange_ids=()),
                RetrievalResult(query_id="q2", ranked_exchange_ids=()),
            ),
        ),
    ]
    scores = score_runs(runs, queries)
    assert scores[0].mrr_at_10 == 0.0
    assert scores[0].recall_at[10] == 0.0


def test_render_markdown_has_no_baseline_column() -> None:
    queries = _queries()
    runs = [
        AdapterRunResult(
            adapter_id="symbol",
            results=(
                RetrievalResult(query_id="q1", ranked_exchange_ids=("e1",)),
                RetrievalResult(query_id="q2", ranked_exchange_ids=("e2", "e3")),
            ),
        ),
    ]
    scores = score_runs(runs, queries)
    corpus = CorpusStats(exchange_count=42, session_count=3, query_count=2)

    table = render_markdown(scores, corpus, seed=42)

    assert "vs grep" not in table
    assert "baseline" not in table.lower()
    assert "42 exchanges" in table
    assert "seed=42" in table
    assert "symbol" in table


def test_render_json_omits_baseline_field() -> None:
    queries = _queries()
    runs = [
        AdapterRunResult(
            adapter_id="symbol",
            results=(
                RetrievalResult(query_id="q1", ranked_exchange_ids=("e1",)),
                RetrievalResult(query_id="q2", ranked_exchange_ids=()),
            ),
        ),
    ]
    scores = score_runs(runs, queries)
    corpus = CorpusStats(exchange_count=1, session_count=1, query_count=2)

    payload = json.loads(render_json(scores, corpus, seed=7))

    assert payload["seed"] == 7
    assert payload["adapters"][0]["adapter_id"] == "symbol"
    assert "baseline_mrr_delta" not in payload["adapters"][0]


def test_corpus_stats_counts_exchanges_and_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    con.execute("INSERT INTO conversations (id, source_path) VALUES ('c1', '/p')")
    con.execute(
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) "
        "VALUES ('e1', 'c1', 0, 1, 'u', 'a')"
    )
    con.commit()
    con.close()

    stats = corpus_stats(db_path, _queries())

    assert stats.exchange_count == 1
    assert stats.query_count == 2
