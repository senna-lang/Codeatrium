"""context_lookup のテスト（design §6.0・§6.1・§6.2）"""

from __future__ import annotations

from pathlib import Path

from codeatrium.context_lookup import (
    ContextTarget,
    parse_context_target,
    pick_enclosing_symbol_name,
    resolve_u1,
    resolve_u2,
)
from codeatrium.db import get_connection, init_db

LONG = "x" * 200


# ---- parse_context_target（design §6.1） ----


def test_parse_context_target_file_only() -> None:
    assert parse_context_target("src/foo.py") == ContextTarget(file_path="src/foo.py")


def test_parse_context_target_file_and_symbol() -> None:
    assert parse_context_target("src/foo.py:greet") == ContextTarget(
        file_path="src/foo.py", symbol_name="greet"
    )


def test_parse_context_target_file_and_line() -> None:
    assert parse_context_target("src/foo.py:142") == ContextTarget(
        file_path="src/foo.py", line=142
    )


def test_parse_context_target_dotted_symbol_name() -> None:
    """メソッド名は Foo.bar の形（design §2.4「ファイル名＋シンボル名」）"""
    assert parse_context_target("src/foo.py:Foo.bar") == ContextTarget(
        file_path="src/foo.py", symbol_name="Foo.bar"
    )


def test_parse_context_target_absolute_path_no_symbol() -> None:
    """agent が Read/Edit から得るのは絶対パスであることが多い（U2）"""
    assert parse_context_target("/repo/src/foo.py") == ContextTarget(
        file_path="/repo/src/foo.py"
    )


def test_parse_context_target_absolute_path_with_symbol() -> None:
    assert parse_context_target("/repo/src/foo.py:greet") == ContextTarget(
        file_path="/repo/src/foo.py", symbol_name="greet"
    )


def test_parse_context_target_colon_inside_path_is_not_a_separator() -> None:
    """コロンの後ろに "/" があれば、それはパスの一部（POSIXではファイル名にコロンを含められる）"""
    assert parse_context_target("src/a:b/foo.py") == ContextTarget(
        file_path="src/a:b/foo.py"
    )


# ---- pick_enclosing_symbol_name（design §6.1 行→シンボル変換） ----


def test_pick_enclosing_symbol_name_line_inside_span() -> None:
    symbols = [("greet", 5, 8), ("other", 20, 25)]
    assert pick_enclosing_symbol_name(6, symbols) == "greet"


def test_pick_enclosing_symbol_name_line_outside_all_spans() -> None:
    symbols = [("greet", 5, 8), ("other", 20, 25)]
    assert pick_enclosing_symbol_name(12, symbols) is None


def test_pick_enclosing_symbol_name_boundary_lines_included() -> None:
    symbols = [("greet", 5, 8)]
    assert pick_enclosing_symbol_name(5, symbols) == "greet"
    assert pick_enclosing_symbol_name(8, symbols) == "greet"


def test_pick_enclosing_symbol_name_empty_symbols_returns_none() -> None:
    assert pick_enclosing_symbol_name(5, []) is None


# ---- resolve_u1 / resolve_u2 のフィクスチャ ----


def _setup_db(tmp_path: Path):
    db = tmp_path / "memory.db"
    init_db(db)
    return get_connection(db)


def _insert_conversation_and_exchange(
    con, conv_id: str, ex_id: str, ply_start: int = 0, git_branch: str | None = None
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?, ?)",
        (conv_id, f"/fake/{conv_id}.jsonl"),
    )
    con.execute(
        """INSERT OR IGNORE INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content, git_branch)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ex_id, conv_id, ply_start, ply_start + 1, "user " + LONG, "agent " + LONG, git_branch),
    )


def _insert_symbol(con, symbol_id: str, file_path: str, symbol_name: str) -> None:
    con.execute(
        """INSERT OR IGNORE INTO code_symbols
           (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at)
           VALUES (?, ?, ?, 'function', 'def f():', 1, 2, '.py', '2026-08-09T00:00:00Z')""",
        (symbol_id, file_path, symbol_name),
    )


def _insert_edge(
    con,
    edge_id: str,
    exchange_id: str,
    file_path: str,
    symbol_id: str | None,
    granularity: str,
    confidence: float,
    ts: str = "2026-08-09T00:00:00Z",
) -> None:
    con.execute(
        """INSERT OR IGNORE INTO code_edges
           (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts)
           VALUES (?, ?, ?, ?, 'edit', ?, ?, 1, ?)""",
        (edge_id, exchange_id, file_path, symbol_id, granularity, confidence, ts),
    )


# ---- resolve_u1 ----


def test_resolve_u1_exact_symbol_match(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_symbol(con, "sym1", "src/foo.py", "greet")
    _insert_edge(con, "e1", "ex1", "src/foo.py", "sym1", "line", 1.0)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert len(hits) == 1
    assert hits[0].match_kind == "symbol"
    assert hits[0].confidence == 1.0
    assert hits[0].symbol_name == "greet"
    assert hits[0].exchange_id == "ex1"
    assert hits[0].verbatim_ref == "/fake/c1.jsonl:ply=0"


def test_resolve_u1_falls_back_to_file_when_symbol_not_found(tmp_path: Path) -> None:
    """不変条件: code_touches.symbol_name を誤って参照していないか
    （resolve_symbol_name はまだ配線されていないので常にNULLのはず）"""
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_symbol(con, "sym1", "src/foo.py", "other_func")
    _insert_edge(con, "e1", "ex1", "src/foo.py", "sym1", "line", 1.0)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert len(hits) == 1
    assert hits[0].match_kind == "file"
    assert hits[0].confidence == 0.45


def test_resolve_u1_falls_back_to_directory(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_edge(con, "e1", "ex1", "src/bar.py", None, "file", 0.5)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert len(hits) == 1
    assert hits[0].match_kind == "directory"
    assert hits[0].confidence == 0.25
    assert hits[0].file_path == "src/bar.py"


def test_resolve_u1_directory_match_excludes_nested_subdirectories(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_edge(con, "e1", "ex1", "src/nested/bar.py", None, "file", 0.5)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert hits == []


def test_resolve_u1_no_match_returns_empty(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert hits == []


def test_resolve_u1_does_not_top_up_across_tiers(tmp_path: Path) -> None:
    """symbol段がヒットしたら、limitに満たなくてもfile段から埋め合わせない（design §6.2）"""
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_symbol(con, "sym1", "src/foo.py", "greet")
    _insert_edge(con, "e1", "ex1", "src/foo.py", "sym1", "line", 1.0)
    # 同じファイルの別シンボルへの file 段ヒット候補（symbol段がヒットするので使われないはず）
    _insert_conversation_and_exchange(con, "c2", "ex2")
    _insert_symbol(con, "sym2", "src/foo.py", "other")
    _insert_edge(con, "e2", "ex2", "src/foo.py", "sym2", "line", 1.0)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert len(hits) == 1
    assert all(h.match_kind == "symbol" for h in hits)


def test_resolve_u1_respects_limit(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    for i in range(3):
        _insert_conversation_and_exchange(con, f"c{i}", f"ex{i}")
        _insert_symbol(con, "sym1", "src/foo.py", "greet")
        _insert_edge(con, f"e{i}", f"ex{i}", "src/foo.py", "sym1", "line", 1.0)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=2)

    assert len(hits) == 2


def test_resolve_u1_alias_paths_widen_symbol_tier(tmp_path: Path) -> None:
    """design §8.2: 旧パスの edge も symbol段でヒットする（file段へ落とさない）"""
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_symbol(con, "sym1", "src/logo/db.py", "get_connection")
    _insert_edge(con, "e1", "ex1", "src/logo/db.py", "sym1", "line", 1.0)
    con.commit()

    hits = resolve_u1(
        con, "src/codeatrium/db.py", "get_connection", limit=5,
        alias_paths=("src/logo/db.py",),
    )

    assert len(hits) == 1
    assert hits[0].match_kind == "symbol"
    assert hits[0].confidence == 1.0


def test_resolve_u1_alias_paths_do_not_affect_lookup_when_empty(tmp_path: Path) -> None:
    """alias_paths を渡さない場合の挙動は変わらない（既定は空タプル）"""
    con = _setup_db(tmp_path)
    con.commit()

    hits = resolve_u1(con, "src/foo.py", "greet", limit=5)

    assert hits == []


# ---- resolve_u2 ----


def test_resolve_u2_file_match_groups_multiple_symbols(tmp_path: Path) -> None:
    """U2 file段: シンボルごとにまとめて複数返す（design §6.2、best 1件だけを返さない）"""
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_symbol(con, "sym1", "src/foo.py", "greet")
    _insert_edge(con, "e1", "ex1", "src/foo.py", "sym1", "line", 1.0)
    _insert_conversation_and_exchange(con, "c2", "ex2")
    _insert_symbol(con, "sym2", "src/foo.py", "farewell")
    _insert_edge(con, "e2", "ex2", "src/foo.py", "sym2", "line", 1.0)
    con.commit()

    hits = resolve_u2(con, "src/foo.py", limit=5)

    assert len(hits) == 2
    assert all(h.match_kind == "file" for h in hits)
    assert all(h.confidence == 1.0 for h in hits)
    assert {h.symbol_name for h in hits} == {"greet", "farewell"}


def test_resolve_u2_falls_back_to_directory(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_edge(con, "e1", "ex1", "src/bar.py", None, "file", 0.5)
    con.commit()

    hits = resolve_u2(con, "src/foo.py", limit=5)

    assert len(hits) == 1
    assert hits[0].match_kind == "directory"
    assert hits[0].confidence == 0.30


def test_resolve_u2_no_match_returns_empty(tmp_path: Path) -> None:
    con = _setup_db(tmp_path)
    con.commit()

    hits = resolve_u2(con, "src/foo.py", limit=5)

    assert hits == []


def test_resolve_u2_root_level_file_directory_match(tmp_path: Path) -> None:
    """ディレクトリが '' (プロジェクトルート直下) のケースを正しく扱う"""
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_edge(con, "e1", "ex1", "README.md", None, "file", 0.5)
    con.commit()

    hits = resolve_u2(con, "pyproject.toml", limit=5)

    assert len(hits) == 1
    assert hits[0].match_kind == "directory"
    assert hits[0].file_path == "README.md"


def test_resolve_u2_alias_paths_widen_file_tier(tmp_path: Path) -> None:
    """design §8.2: 旧パスの edge を file段でひとつのファイルとして扱う"""
    con = _setup_db(tmp_path)
    _insert_conversation_and_exchange(con, "c1", "ex1")
    _insert_edge(con, "e1", "ex1", "src/logo/db.py", None, "file", 0.5)
    con.commit()

    hits = resolve_u2(
        con, "src/codeatrium/db.py", limit=5, alias_paths=("src/logo/db.py",)
    )

    assert len(hits) == 1
    assert hits[0].match_kind == "file"
    assert hits[0].confidence == 1.0
    assert hits[0].file_path == "src/logo/db.py"
