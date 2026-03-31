"""
DB 初期化・スキーマのテスト
"""

import sqlite3
from pathlib import Path

from codeatrium.db import get_connection, init_db


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
