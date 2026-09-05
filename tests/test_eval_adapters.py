"""SymbolAdapter — the code→conversation lookup feature under test (E3)."""

from __future__ import annotations

from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.eval.adapters.symbol import SymbolAdapter
from codeatrium.eval.datasets.schema import Query


def test_symbol_adapter_resolves_via_code_edges(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    con.execute("INSERT INTO conversations (id, source_path) VALUES ('c1', '/path/c1.jsonl')")
    con.execute(
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) "
        "VALUES ('ex1', 'c1', 0, 1, 'fix list_dir', 'done')"
    )
    con.execute(
        "INSERT INTO code_symbols (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at) "
        "VALUES ('sym1', 'src/foo.py', 'list_dir', 'function', 'def list_dir():', 1, 2, '.py', '2026-01-01')"
    )
    con.execute(
        "INSERT INTO code_edges (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts) "
        "VALUES ('edge1', 'ex1', 'src/foo.py', 'sym1', 'edit', 'line', 1.0, 3, '2026-01-01')"
    )
    con.commit()
    con.close()

    query = Query(
        id="q1", kind="symbol", value="src/foo.py::list_dir", gold_exchange_ids=("ex1",)
    )
    assert SymbolAdapter(db_path).retrieve(query, k=5) == ["ex1"]


def test_symbol_adapter_truncates_to_k(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    con.execute(
        "INSERT INTO code_symbols (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at) "
        "VALUES ('sym1', 'src/foo.py', 'list_dir', 'function', 'def list_dir():', 1, 2, '.py', '2026-01-01')"
    )
    for i in range(3):
        ex_id = f"ex{i}"
        con.execute(
            "INSERT INTO conversations (id, source_path) VALUES (?, ?)", (f"c{i}", f"/p{i}")
        )
        con.execute(
            "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content, git_branch) "
            "VALUES (?, ?, 0, 1, 'fix', 'done', 'main')",
            (ex_id, f"c{i}"),
        )
        con.execute(
            "INSERT INTO code_edges (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts) "
            "VALUES (?, ?, 'src/foo.py', 'sym1', 'edit', 'line', 1.0, 1, ?)",
            (f"edge{i}", ex_id, f"2026-01-0{i + 1}"),
        )
    con.commit()
    con.close()

    query = Query(
        id="q1", kind="symbol", value="src/foo.py::list_dir", gold_exchange_ids=("ex0",)
    )
    assert len(SymbolAdapter(db_path).retrieve(query, k=2)) == 2


def test_symbol_adapter_rejects_non_symbol_queries(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    query = Query(id="q1", kind="text", value="list_dir", gold_exchange_ids=("ex1",))
    assert SymbolAdapter(db_path).retrieve(query, k=5) == []


def test_symbol_adapter_no_edges_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    query = Query(
        id="q1", kind="symbol", value="src/foo.py::never_touched", gold_exchange_ids=("ex1",)
    )
    assert SymbolAdapter(db_path).retrieve(query, k=5) == []
