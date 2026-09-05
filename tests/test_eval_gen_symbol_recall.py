"""symbol-recall gold construction — edit+text grounded, no `symbols` table (E2)."""

from __future__ import annotations

from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.eval.gen.gen_symbol_recall import (
    _index_edited_files,
    generate_symbol_recall_queries,
    gold_for_symbol,
)
from codeatrium.resolver import Symbol


def _seed_exchange(
    con, ex_id, conv_id, file_path, user_content, agent_content, git_branch=None, edited=True
):
    """Seed an exchange that touched `file_path`. `edited=False` seeds a
    read-only touch (no `code_touches` row) — must never contribute gold."""
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?, ?)",
        (conv_id, f"/path/{conv_id}.jsonl"),
    )
    con.execute(
        """
        INSERT INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content, git_branch)
        VALUES (?, ?, 0, 1, ?, ?, ?)
        """,
        (ex_id, conv_id, user_content, agent_content, git_branch),
    )
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        (ex_id, file_path),
    )
    if edited:
        con.execute(
            """
            INSERT INTO code_touches
                (id, exchange_id, harness, tool_call_id, file_path, touch_kind, locator_kind)
            VALUES (?, ?, 'claude', ?, ?, 'edit', 'file_only')
            """,
            (f"touch-{ex_id}", ex_id, f"call-{ex_id}", file_path),
        )


def _rows_for(con, rel_path: str):
    return _index_edited_files(con).get(rel_path, [])


def _symbol(name: str, project_root: Path, rel_path: str) -> Symbol:
    return Symbol(
        symbol_name=name,
        symbol_kind="function",
        signature=f"def {name}():",
        line=1,
        end_line=2,
        file_path=str(project_root / rel_path),
        lang=".py",
    )


def test_gold_for_symbol_requires_file_and_text_match(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "let's fix list_dir", "done")
    _seed_exchange(con, "ex2", "c2", "src/foo.py", "unrelated question", "unrelated answer")
    _seed_exchange(con, "ex3", "c3", "src/bar.py", "list_dir mentioned here too", "ok")
    con.commit()

    gold = gold_for_symbol(_rows_for(con, "src/foo.py"), "list_dir", allowed_branches=None)
    con.close()

    assert gold == ["ex1"]


def test_gold_for_symbol_excludes_read_only_touches(tmp_path: Path) -> None:
    """An exchange that only read the file (no edit) must not contribute gold —
    `loci context <file>:<symbol>` promises git-blame semantics, not "who read this"."""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "reading list_dir here", "ok", edited=False)
    con.commit()

    gold = gold_for_symbol(_rows_for(con, "src/foo.py"), "list_dir", allowed_branches=None)
    con.close()

    assert gold == []


def test_gold_for_symbol_matches_leaf_of_dotted_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "call bar on the instance", "done")
    con.commit()

    gold = gold_for_symbol(_rows_for(con, "src/foo.py"), "Foo.bar", allowed_branches=None)
    con.close()

    assert gold == ["ex1"]


def test_gold_for_symbol_excludes_known_branch_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "list_dir change", "done", git_branch="main")
    _seed_exchange(con, "ex2", "c2", "src/foo.py", "list_dir change", "done", git_branch="other")
    con.commit()

    gold = gold_for_symbol(
        _rows_for(con, "src/foo.py"), "list_dir", allowed_branches=frozenset({"main"})
    )
    con.close()

    assert gold == ["ex1"]


def test_gold_for_symbol_keeps_unknown_branch_when_filtering(tmp_path: Path) -> None:
    """git_branch=NULL means "we don't know" — must not be treated as excluded."""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "list_dir change", "done", git_branch=None)
    con.commit()

    gold = gold_for_symbol(
        _rows_for(con, "src/foo.py"), "list_dir", allowed_branches=frozenset({"main"})
    )
    con.close()

    assert gold == ["ex1"]


def test_generate_symbol_recall_queries_filters_by_gold_count(tmp_path: Path) -> None:
    """0-gold and over-max-gold symbols are excluded; the app's own `symbols`
    table is never touched (only `symbols=[...]` injected here + git_history
    stub bypass what would otherwise call tree-sitter/git)."""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    project_root = tmp_path
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "list_dir change", "done")
    _seed_exchange(con, "ex2", "c2", "src/bar.py", "helper call one", "done")
    _seed_exchange(con, "ex3", "c3", "src/bar.py", "helper call two", "done")
    con.commit()

    symbols = [
        _symbol("list_dir", project_root, "src/foo.py"),
        _symbol("never_mentioned", project_root, "src/foo.py"),
        _symbol("helper", project_root, "src/bar.py"),
    ]

    queries = generate_symbol_recall_queries(
        con,
        project_root,
        min_gold=1,
        max_gold=1,
        symbols=symbols,
        allowed_branches_fn=lambda *_: None,
    )
    con.close()

    values = {q.value: q.gold_exchange_ids for q in queries}
    assert values == {"src/foo.py::list_dir": ("ex1",)}


def test_generate_symbol_recall_queries_deduplicates_same_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    project_root = tmp_path
    con = get_connection(db_path)
    _seed_exchange(con, "ex1", "c1", "src/foo.py", "list_dir change", "done")
    con.commit()

    symbols = [
        _symbol("list_dir", project_root, "src/foo.py"),
        _symbol("list_dir", project_root, "src/foo.py"),
    ]

    queries = generate_symbol_recall_queries(
        con, project_root, symbols=symbols, allowed_branches_fn=lambda *_: None
    )
    con.close()

    assert len(queries) == 1


def test_generate_symbol_recall_queries_skips_files_outside_project(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    con = get_connection(db_path)

    symbols = [
        Symbol(
            symbol_name="list_dir",
            symbol_kind="function",
            signature="def list_dir():",
            line=1,
            end_line=2,
            file_path="/somewhere/else/foo.py",
            lang=".py",
        ),
    ]

    queries = generate_symbol_recall_queries(
        con, project_root, symbols=symbols, allowed_branches_fn=lambda *_: None
    )
    con.close()

    assert queries == []
