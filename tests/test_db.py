"""
DB 初期化・スキーマのテスト
"""

import sqlite3
from pathlib import Path

from codeatrium.db import _MIGRATIONS, get_connection, init_db


def test_init_db_creates_conversations_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_creates_exchanges_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_creates_exchanges_fts(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges_fts'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    # 2回呼んでもエラーにならない
    init_db(db_path)


def test_get_connection_returns_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    assert con is not None
    con.close()


def test_init_db_creates_vec_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = get_connection(db_path)
    cur = con.execute("SELECT name FROM sqlite_master WHERE name='vec_exchanges'")
    assert cur.fetchone() is not None
    con.close()


def test_init_db_stamps_user_version_on_new_db(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(_MIGRATIONS)
    con.close()


def test_init_db_migration_adds_last_ply_end_to_legacy_db(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with legacy schema (no last_ply_end)
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP
        )"""
    )
    raw_con.execute("INSERT INTO conversations(id, source_path) VALUES ('row1', '/a')")
    raw_con.execute("PRAGMA user_version = 0")
    raw_con.commit()
    raw_con.close()

    # Now call init_db which should run migrations
    init_db(db_path)

    # Verify migration was applied
    con = sqlite3.connect(db_path)
    table_info = con.execute("PRAGMA table_info(conversations)").fetchall()
    column_names = [col[1] for col in table_info]
    assert "last_ply_end" in column_names

    # Verify inserted row still exists
    row = con.execute(
        "SELECT id, source_path FROM conversations WHERE id='row1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "row1"
    assert row[1] == "/a"

    # Verify user_version was stamped
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(_MIGRATIONS)
    con.close()


def test_init_db_migration_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    init_db(db_path)

    con = sqlite3.connect(db_path)
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == len(_MIGRATIONS)
    con.close()


def test_get_connection_journal_mode_is_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = get_connection(db_path)
    journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode.lower() == "wal"
    con.close()


def test_concurrent_writes_do_not_raise_locked(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con1 = get_connection(db_path)
    con2 = get_connection(db_path)

    con1.execute("INSERT INTO conversations(id, source_path) VALUES ('id1', '/path1')")
    con1.commit()

    con2.execute("INSERT INTO conversations(id, source_path) VALUES ('id2', '/path2')")
    con2.commit()

    con1.close()
    con2.close()
