"""loci context コマンドの出力契約テスト

C5: 既定出力から会話全文を外し verbatim_ref を返す。--full で全文復元。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.cli.search_cmd import _semantic_query_text
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


def test_search_json_has_exchange_id_field(tmp_path, monkeypatch):
    """design: exchange_id が無いと loci show へ繋げられず、周辺コンテキストの
    traversal に入れない（PRIME_TEXT の show 例が指す先）"""
    from unittest.mock import MagicMock, patch

    from codeatrium.models import FusedResult

    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_fixture(con)
    con.close()

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
    )

    with (
        patch("codeatrium.embedder.Embedder", return_value=MagicMock()),
        patch("codeatrium.search.search_combined", return_value=[mock_result]),
    ):
        result = runner.invoke(app, ["search", "test query", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["exchange_id"] == "ex1"


# ---- U1/U2 位置引数（design §6.1・§6.2） ----


def _insert_code_edge_fixture(
    con: sqlite3.Connection,
    ex_id: str = "ex1",
    conv_id: str = "conv1",
    file_path: str = "src/foo.py",
    symbol_name: str | None = "greet",
    granularity: str = "line",
    confidence: float = 1.0,
    ply_start: int = 0,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?, ?)",
        (conv_id, "/fake/session.jsonl"),
    )
    con.execute(
        """INSERT OR IGNORE INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ex_id, conv_id, ply_start, ply_start + 1, "user " + LONG, "agent " + LONG),
    )
    symbol_id = None
    if symbol_name is not None:
        symbol_id = f"sym-{symbol_name}"
        con.execute(
            """INSERT OR IGNORE INTO code_symbols
               (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at)
               VALUES (?, ?, ?, 'function', 'def f():', 1, 2, '.py', '2026-08-09T00:00:00Z')""",
            (symbol_id, file_path, symbol_name),
        )
    con.execute(
        """INSERT OR IGNORE INTO code_edges
           (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts)
           VALUES (?, ?, ?, ?, 'edit', ?, ?, 1, '2026-08-09T00:00:00Z')""",
        (f"edge-{ex_id}-{file_path}-{symbol_name}", ex_id, file_path, symbol_id, granularity, confidence),
    )
    con.commit()


def test_context_u1_symbol_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "src/foo.py:greet", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["match_kind"] == "symbol"
    assert data[0]["confidence"] == 1.0
    assert data[0]["symbol_name"] == "greet"
    assert "user_content" not in data[0]


def test_context_u1_full_flag_includes_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "src/foo.py:greet", "--json", "--full"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "user_content" in data[0]
    assert "agent_content" in data[0]


def test_context_u2_file_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)
    con.close()

    result = runner.invoke(app, ["context", "src/foo.py", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["match_kind"] == "file"
    assert data[0]["confidence"] == 1.0


def test_context_line_resolves_to_enclosing_symbol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)  # symbol spans line=1..end_line=2
    con.close()

    result = runner.invoke(app, ["context", "src/foo.py:1", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["match_kind"] == "symbol"
    assert data[0]["symbol_name"] == "greet"


def test_context_absolute_path_is_normalized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)
    con.close()

    abs_target = str(tmp_path / "src" / "foo.py") + ":greet"
    result = runner.invoke(app, ["context", abs_target, "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["file_path"] == "src/foo.py"


def test_context_absolute_path_outside_project_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    con.close()

    result = runner.invoke(app, ["context", "/somewhere/else/foo.py:greet"])
    assert result.exit_code == 1


def test_context_positional_target_takes_precedence_over_symbol_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)
    con.close()

    result = runner.invoke(
        app, ["context", "src/foo.py:greet", "--symbol", "SomethingElse", "--json"]
    )
    assert result.exit_code == 0
    assert "Warning" in result.stderr
    data = json.loads(result.stdout)
    assert data[0]["symbol_name"] == "greet"


def test_context_u1_no_edges_falls_back_to_semantic(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from codeatrium.models import FusedResult

    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    con.close()

    mock_result = FusedResult(
        exchange_id="ex1",
        user_content="user content",
        agent_content="agent content",
        score=0.5,
        exchange_core="core summary",
        specific_context="specific detail",
        verbatim_ref="/fake/session.jsonl:ply=0",
        rooms=[],
        symbols=[],
        git_branch=None,
    )

    with (
        patch("codeatrium.embedder.Embedder", return_value=MagicMock()),
        patch("codeatrium.search.search_combined", return_value=[mock_result]),
    ):
        result = runner.invoke(app, ["context", "src/nomatch.py:missing", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["match_kind"] == "semantic"
        assert data[0]["confidence"] == 0.10


def test_context_no_results_at_all(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    con.close()

    with (
        patch("codeatrium.embedder.Embedder", return_value=MagicMock()),
        patch("codeatrium.search.search_combined", return_value=[]),
    ):
        result = runner.invoke(app, ["context", "src/nomatch.py:missing"])
        assert result.exit_code == 0
        assert "No results found." in result.output


def test_context_not_initialized_exits_1_for_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["context", "src/foo.py:greet"])
    assert result.exit_code == 1


def test_context_u1_line_miss_falls_back_to_u2(tmp_path, monkeypatch):
    """行がどのシンボルにも含まれなければ U2（ファイル単位）へ落ちる（design §6.1）"""
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_code_edge_fixture(con)  # greet は line=1..end_line=2
    con.close()

    result = runner.invoke(app, ["context", "src/foo.py:999", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["match_kind"] == "file"


# ---- _semantic_query_text（design §6.2 最終段のクエリ文字列） ----


def test_semantic_query_text_u1_includes_symbol_and_module_stem():
    text = _semantic_query_text("src/codeatrium/config.py", "Config")
    assert "Config" in text
    assert "config" in text
    assert ".py" not in text


def test_semantic_query_text_u2_includes_module_stem_and_path():
    text = _semantic_query_text("src/codeatrium/config.py", None)
    assert "config" in text
    assert "src/codeatrium/config.py" in text
