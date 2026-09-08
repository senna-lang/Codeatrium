"""`loci gc` の DB 清掃・バックアップ保持・スキーマ移行の観測可能な契約。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db

runner = CliRunner()


def _setup_db(tmp_path: Path) -> Path:
    db = tmp_path / ".codeatrium" / "memory.db"
    db.parent.mkdir()
    init_db(db)
    return db


def _insert_exchange(con: sqlite3.Connection, exchange_id: str = "ex-live") -> None:
    con.execute(
        "INSERT INTO conversations(id, source_path) VALUES ('conv-live', '/sessions/live.jsonl')"
    )
    con.execute(
        """INSERT INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content)
           VALUES (?, 'conv-live', 0, 1, 'user', 'agent')""",
        (exchange_id,),
    )


def test_gc_removes_only_orphans_vacuums_and_rotates_backups(
    tmp_path: Path, monkeypatch
) -> None:
    """GC は live 行を保持し、孤立行・余剰 DB backup だけを消す。"""
    monkeypatch.chdir(tmp_path)
    db = _setup_db(tmp_path)
    con = get_connection(db)
    _insert_exchange(con)
    con.execute(
        """INSERT INTO palace_objects
           (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES ('palace-live', 'ex-live', 'live', 'live', 'live')"""
    )
    con.execute(
        """INSERT INTO palace_objects
           (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES ('palace-orphan', 'ex-missing', 'orphan', 'orphan', 'orphan')"""
    )
    con.execute(
        """INSERT INTO rooms
           (id, palace_object_id, room_type, room_key, room_label, relevance, dedup_hash)
           VALUES ('room-live', 'palace-live', 'file', 'live.py', 'live', 1, 'live')"""
    )
    con.execute(
        """INSERT INTO rooms
           (id, palace_object_id, room_type, room_key, room_label, relevance, dedup_hash)
           VALUES ('room-orphan', 'palace-missing', 'file', 'gone.py', 'gone', 1, 'gone')"""
    )
    vector = bytes(384 * 4)
    con.execute(
        "INSERT INTO vec_palace(palace_id, embedding) VALUES ('palace-live', ?)",
        (vector,),
    )
    con.execute(
        "INSERT INTO vec_palace(palace_id, embedding) VALUES ('palace-missing', ?)",
        (vector,),
    )
    con.execute(
        """INSERT INTO sessions
           (id, harness, source_session_id, primary_ref, project_key, updated_at)
           VALUES ('session-orphan', 'claude', 'missing', '/sessions/missing.jsonl', '', '2026-01-01')"""
    )
    con.execute(
        """INSERT INTO code_touches
           (id, exchange_id, harness, tool_call_id, file_path, touch_kind, locator_kind)
           VALUES ('touch-orphan', 'ex-missing', 'claude', 'call', 'gone.py', 'edit', 'line')"""
    )
    con.execute(
        """INSERT INTO code_edges
           (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence)
           VALUES ('edge-orphan', 'ex-missing', 'gone.py', NULL, 'edit', 'file', 0.4)"""
    )
    con.execute(
        "INSERT INTO exchange_files(exchange_id, file_path) VALUES ('ex-missing', 'gone.py')"
    )
    con.commit()
    con.close()

    current_backup = db.with_name("memory.db.bak")
    current_backup.write_bytes(b"old backup")
    archives = [db.with_name(f"memory.db.bak.20240101T00000{i}") for i in range(4)]
    for i, archive in enumerate(archives):
        archive.write_bytes(b"old archive")
        os.utime(archive, (i + 1, i + 1))

    result = runner.invoke(app, ["gc"])

    assert result.exit_code == 0, result.output
    assert "palace_objects=1" in result.output
    assert "vec_palace=1" in result.output
    assert "sessions=1" in result.output
    assert current_backup.exists()
    archived_backups = list(db.parent.glob("memory.db.bak.*"))
    assert len(archived_backups) == 3
    assert any(archive.read_bytes() == b"old backup" for archive in archived_backups)

    con = get_connection(db)
    assert con.execute("SELECT COUNT(*) FROM palace_objects").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM vec_palace").fetchone()[0] == 1
    assert (
        con.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 'session-orphan'"
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_touches WHERE id = 'touch-orphan'"
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_edges WHERE id = 'edge-orphan'"
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM exchange_files WHERE exchange_id = 'ex-missing'"
        ).fetchone()[0]
        == 0
    )
    con.close()

    backup_con = sqlite3.connect(current_backup)
    assert backup_con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert backup_con.execute("SELECT COUNT(*) FROM palace_objects").fetchone()[0] == 2
    backup_con.close()


def test_init_db_migrates_dead_schema_without_losing_symbol_search_data(
    tmp_path: Path,
) -> None:
    """v14 は旧 symbols の live 関係を code edge へ移し、デッド schema を drop する。"""
    db = _setup_db(tmp_path)
    con = get_connection(db)
    _insert_exchange(con)
    con.execute(
        """INSERT INTO palace_objects
           (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES ('palace-live', 'ex-live', 'live', 'live', 'live')"""
    )
    con.execute(
        """CREATE TABLE symbols (
            id TEXT PRIMARY KEY,
            palace_object_id TEXT NOT NULL,
            symbol_name TEXT NOT NULL,
            symbol_kind TEXT NOT NULL,
            file_path TEXT NOT NULL,
            signature TEXT NOT NULL,
            line INT NOT NULL,
            dedup_hash TEXT NOT NULL
        )"""
    )
    con.execute(
        "CREATE TABLE vec_exchanges (exchange_id TEXT PRIMARY KEY, embedding BLOB)"
    )
    con.execute(
        """INSERT INTO symbols
           (id, palace_object_id, symbol_name, symbol_kind, file_path, signature, line, dedup_hash)
           VALUES ('legacy-symbol', 'palace-live', 'greet', 'function', 'src/greet.py',
                   'def greet()', 4, 'legacy')"""
    )
    con.execute("PRAGMA user_version = 13")
    con.commit()
    con.close()

    init_db(db)

    con = get_connection(db)
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "symbols" not in tables
    assert "vec_exchanges" not in tables
    columns = {
        row[1] for row in con.execute("PRAGMA table_info(code_touches)").fetchall()
    }
    assert "symbol_name" not in columns
    assert "resolved_by" not in columns
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_symbols WHERE file_path = 'src/greet.py' AND symbol_name = 'greet'"
        ).fetchone()[0]
        == 1
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_edges WHERE exchange_id = 'ex-live'"
        ).fetchone()[0]
        == 1
    )
    con.close()
