"""loci context コマンドの出力契約テスト

C5: 既定出力から会話全文を外し verbatim_ref を返す。--full で全文復元。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db

runner = CliRunner()

LONG = "x" * 200


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    db = codeatrium_dir / "memory.db"
    init_db(db)
    con = get_connection(db)
    return db, con


def _insert_fixture(
    con,
    ex_id="ex1",
    conv_id="conv1",
    source_path="/fake/session.jsonl",
    ply_start=0,
    symbol_name="MyFunc",
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?,?)",
        (conv_id, source_path),
    )
    con.execute(
        """INSERT OR IGNORE INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content)
           VALUES (?,?,?,?,?,?)""",
        (ex_id, conv_id, ply_start, ply_start + 3, "user " + LONG, "agent " + LONG),
    )
    con.execute(
        """INSERT OR IGNORE INTO palace_objects
           (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES (?,?,?,?,?)""",
        ("p1", ex_id, "core summary", "specific detail", "core summary"),
    )
    con.execute(
        """INSERT OR IGNORE INTO symbols
           (id, palace_object_id, symbol_name, symbol_kind, file_path,
            signature, line, dedup_hash)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("s1", "p1", symbol_name, "function", "src/foo.py", "def MyFunc()", 42, "hash1"),
    )
    con.commit()


def test_context_default_no_full_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "--symbol", "MyFunc", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "user_content" not in data[0]
    assert "agent_content" not in data[0]
    assert "verbatim_ref" in data[0]


def test_context_full_flag_includes_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

    result = runner.invoke(
        app, ["context", "--symbol", "MyFunc", "--json", "--full"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "user_content" in data[0]
    assert "agent_content" in data[0]


def test_context_default_symbol_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "--symbol", "MyFunc", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert set(data[0].keys()) == {
        "symbol_name",
        "symbol_kind",
        "file_path",
        "signature",
        "line",
        "exchange_id",
        "exchange_core",
        "specific_context",
        "verbatim_ref",
        "git_branch",
    }


def test_context_verbatim_ref_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con, source_path="/fake/session.jsonl", ply_start=10)
    con.close()

    result = runner.invoke(app, ["context", "--symbol", "MyFunc", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["verbatim_ref"] == "/fake/session.jsonl:ply=10"


def test_context_branch_only_returns_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    # Update git_branch directly
    con.execute("UPDATE exchanges SET git_branch=? WHERE id=?", ("main", "ex1"))
    con.commit()
    con.close()

    result = runner.invoke(app, ["context", "--branch", "main", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) > 0
    assert data[0]["git_branch"] == "main"
    assert "exchange_id" in data[0]


def test_context_no_args_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "--json"])
    assert result.exit_code == 1


def test_context_symbol_has_git_branch_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "--symbol", "MyFunc", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "git_branch" in data[0]


def test_search_json_has_git_branch_field(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from codeatrium.models import FusedResult

    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

    # Create a mock FusedResult with git_branch set
    mock_result = FusedResult(
        exchange_id="ex1",
        user_content="user content",
        agent_content="agent content",
        score=0.95,
        exchange_core="core summary",
        specific_context="specific detail",
        verbatim_ref="/fake/session.jsonl:ply=0",
        rooms=[],
        symbols=[],
        git_branch="feature-branch",
    )

    # Embedder の実体はモデルロードが走り出力を汚すためモックする
    with (
        patch("codeatrium.embedder.Embedder", return_value=MagicMock()),
        patch("codeatrium.search.search_combined", return_value=[mock_result]),
    ):
        result = runner.invoke(app, ["search", "test query", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0
        assert "git_branch" in data[0]
        assert data[0]["git_branch"] == "feature-branch"
