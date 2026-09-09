"""loci recall コマンドの出力契約テスト

file/branch の独立 AND フィルタ、context と同じ要約出力（exchange_core /
specific_context / verbatim_ref）、および同一関連度の recency 減衰並び替え。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db
from codeatrium.models import FusedResult

runner = CliRunner()

LONG = "x" * 200


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """recall は search_combined を呼ぶため、モデルロードを避ける。"""
    mock = MagicMock()
    mock.embed.return_value = np.zeros(384, dtype=np.float32)
    monkeypatch.setattr("codeatrium.embedder.Embedder", lambda: mock)


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    db = codeatrium_dir / "memory.db"
    init_db(db)
    return db, get_connection(db)


def _insert_recall_row(
    con: sqlite3.Connection,
    *,
    ex_id: str,
    conv_id: str,
    file_path: str,
    symbol_name: str,
    git_branch: str | None,
    ts: str,
    source_path: str = "/fake/session.jsonl",
    ply_start: int = 0,
    core: str = "core summary",
    specific: str = "specific detail",
    user_content: str | None = None,
    agent_content: str | None = None,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path, started_at) VALUES (?,?,?)",
        (conv_id, source_path, ts),
    )
    con.execute(
        """INSERT OR IGNORE INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content, git_branch)
           VALUES (?,?,?,?,?,?,?)""",
        (
            ex_id,
            conv_id,
            ply_start,
            ply_start + 3,
            user_content or ("user " + LONG),
            agent_content or ("agent " + LONG),
            git_branch,
        ),
    )
    con.execute(
        """INSERT OR IGNORE INTO palace_objects
           (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES (?,?,?,?,?)""",
        (f"p-{ex_id}", ex_id, core, specific, core),
    )
    symbol_id = f"sym-{file_path}-{symbol_name}"
    con.execute(
        """INSERT OR IGNORE INTO code_symbols
           (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at)
           VALUES (?, ?, ?, 'function', 'def f():', 1, 2, '.py', ?)""",
        (symbol_id, file_path, symbol_name, ts),
    )
    con.execute(
        """INSERT OR IGNORE INTO code_edges
           (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts)
           VALUES (?, ?, ?, ?, 'edit', 'line', 1.0, 1, ?)""",
        (f"edge-{ex_id}", ex_id, file_path, symbol_id, ts),
    )
    con.commit()


def _invoke_recall(args: list[str]):
    return runner.invoke(app, ["recall", *args])


def test_recall_file_only_returns_file_hits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _db, con = _setup(tmp_path)
    _insert_recall_row(
        con,
        ex_id="ex-foo",
        conv_id="conv-foo",
        file_path="src/foo.py",
        symbol_name="greet",
        git_branch="main",
        ts="2026-08-01T00:00:00Z",
        source_path="/fake/foo.jsonl",
    )
    _insert_recall_row(
        con,
        ex_id="ex-bar",
        conv_id="conv-bar",
        file_path="src/bar.py",
        symbol_name="other",
        git_branch="main",
        ts="2026-08-02T00:00:00Z",
        source_path="/fake/bar.jsonl",
    )
    con.close()

    result = _invoke_recall(["--file", "src/foo.py", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ids = {row["exchange_id"] for row in data}
    assert "ex-foo" in ids
    assert "ex-bar" not in ids


def test_recall_branch_only_returns_branch_hits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _db, con = _setup(tmp_path)
    _insert_recall_row(
        con,
        ex_id="ex-main",
        conv_id="conv-main",
        file_path="src/foo.py",
        symbol_name="greet",
        git_branch="main",
        ts="2026-08-01T00:00:00Z",
        source_path="/fake/main.jsonl",
    )
    _insert_recall_row(
        con,
        ex_id="ex-feat",
        conv_id="conv-feat",
        file_path="src/foo.py",
        symbol_name="greet2",
        git_branch="feat/33",
        ts="2026-08-02T00:00:00Z",
        source_path="/fake/feat.jsonl",
    )
    con.close()

    result = _invoke_recall(["--branch", "feat/33", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) > 0
    ids = {row["exchange_id"] for row in data}
    assert "ex-feat" in ids
    assert "ex-main" not in ids
    assert data[0]["git_branch"] == "feat/33"


def test_recall_file_and_branch_combined_and_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _db, con = _setup(tmp_path)
    _insert_recall_row(
        con,
        ex_id="ex-foo-main",
        conv_id="conv-foo-main",
        file_path="src/foo.py",
        symbol_name="greet",
        git_branch="main",
        ts="2026-08-01T00:00:00Z",
        source_path="/fake/foo-main.jsonl",
    )
    _insert_recall_row(
        con,
        ex_id="ex-foo-feat",
        conv_id="conv-foo-feat",
        file_path="src/foo.py",
        symbol_name="greet2",
        git_branch="feat/33",
        ts="2026-08-02T00:00:00Z",
        source_path="/fake/foo-feat.jsonl",
    )
    _insert_recall_row(
        con,
        ex_id="ex-bar-main",
        conv_id="conv-bar-main",
        file_path="src/bar.py",
        symbol_name="other",
        git_branch="main",
        ts="2026-08-03T00:00:00Z",
        source_path="/fake/bar-main.jsonl",
    )
    con.close()

    result = _invoke_recall(["--file", "src/foo.py", "--branch", "main", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ids = {row["exchange_id"] for row in data}
    assert ids == {"ex-foo-main"}


def test_recall_json_shape_has_summaries_and_verbatim_ref(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _db, con = _setup(tmp_path)
    _insert_recall_row(
        con,
        ex_id="ex1",
        conv_id="conv1",
        file_path="src/foo.py",
        symbol_name="greet",
        git_branch="main",
        ts="2026-08-01T00:00:00Z",
        ply_start=10,
        core="core summary",
        specific="specific detail",
        source_path="/fake/session.jsonl",
    )
    con.close()

    result = _invoke_recall(["--file", "src/foo.py", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) >= 1
    row = data[0]
    assert "user_content" not in row
    assert "agent_content" not in row
    assert row["exchange_core"] == "core summary"
    assert row["specific_context"] == "specific detail"
    assert row["verbatim_ref"] == "/fake/session.jsonl:ply=10"
    assert row["exchange_id"] == "ex1"


def test_recall_no_file_or_branch_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _db, con = _setup(tmp_path)
    con.close()

    result = _invoke_recall(["--json"])
    assert result.exit_code == 1


def test_recency_decay_reorders_tied_relevance():
    """同一 RRF スコアなら、新しい timestamp の exchange が上に来る。"""
    from codeatrium.search import apply_recency_decay

    def hit(eid: str) -> FusedResult:
        return FusedResult(
            exchange_id=eid,
            user_content="u",
            agent_content="a",
            score=0.5,
        )

    now = datetime(2026, 9, 1, tzinfo=UTC)
    out = apply_recency_decay(
        [hit("old"), hit("new")],
        timestamps={
            "old": datetime(2024, 1, 1, tzinfo=UTC),
            "new": datetime(2026, 8, 1, tzinfo=UTC),
        },
        half_life_days=14.0,
        now=now,
    )
    assert [r.exchange_id for r in out] == ["new", "old"]
    assert out[0].score > out[1].score


def test_search_combined_recency_reorders_tied_bm25(tmp_path: Path) -> None:
    """同じ本文（同一 BM25 関連度）でも recency_half_life_days を渡すと新しい方が先。"""
    from codeatrium.search import search_combined

    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    body = "connection pool " * 10
    _insert_recall_row(
        con,
        ex_id="old",
        conv_id="conv-old",
        file_path="src/pool.py",
        symbol_name="old_fn",
        git_branch="main",
        ts="2020-01-01T00:00:00Z",
        source_path="/fake/old.jsonl",
        user_content=body,
        agent_content=body,
    )
    _insert_recall_row(
        con,
        ex_id="new",
        conv_id="conv-new",
        file_path="src/pool.py",
        symbol_name="new_fn",
        git_branch="main",
        ts="2026-08-01T00:00:00Z",
        source_path="/fake/new.jsonl",
        user_content=body,
        agent_content=body,
    )
    # min_exchanges=2: 各会話にパディングを足す
    for conv_id, ply in (("conv-old", 10), ("conv-new", 10)):
        con.execute(
            """INSERT OR IGNORE INTO exchanges
               (id, conversation_id, ply_start, ply_end, user_content, agent_content)
               VALUES (?,?,?,?,?,?)""",
            (f"_pad_{conv_id}", conv_id, ply, ply + 1, "padding", "padding"),
        )
    con.commit()
    con.close()

    vec = np.ones(384, dtype=np.float32)
    results = search_combined(
        db_path,
        "connection pool",
        vec,
        limit=5,
        recency_half_life_days=14.0,
    )
    ids = [r.exchange_id for r in results]
    assert "new" in ids and "old" in ids
    assert ids.index("new") < ids.index("old")
    new_score = next(r.score for r in results if r.exchange_id == "new")
    old_score = next(r.score for r in results if r.exchange_id == "old")
    assert new_score > old_score


def test_search_combined_default_does_not_require_recency(tmp_path: Path) -> None:
    """既存 search()/context() 呼び出しは recency 引数なしで動く。"""
    from codeatrium.search import search_combined

    db_path = tmp_path / "memory.db"
    init_db(db_path)
    vec = np.ones(384, dtype=np.float32)
    assert search_combined(db_path, "query", vec, limit=5) == []
