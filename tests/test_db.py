"""
DB 初期化・スキーマのテスト
"""

import sqlite3
from pathlib import Path

from codeatrium.db import _MIGRATIONS, check_drift, get_connection, init_db


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


def test_init_db_new_db_has_distill_status_column(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    table_info = con.execute("PRAGMA table_info(exchanges)").fetchall()
    column_names = [col[1] for col in table_info]
    assert "distill_status" in column_names
    con.close()


def test_init_db_new_db_has_meta_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_new_db_has_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    indexes = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_rooms_palace_object_id', 'idx_symbols_palace_object_id', 'idx_palace_objects_exchange_id')"
    ).fetchall()
    index_names = [idx[0] for idx in indexes]
    assert "idx_rooms_palace_object_id" in index_names
    assert "idx_symbols_palace_object_id" in index_names
    assert "idx_palace_objects_exchange_id" in index_names
    con.close()


def test_init_db_new_db_meta_has_embedding_model(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = get_connection(db_path)
    row = con.execute("SELECT value FROM meta WHERE key='embedding_model'").fetchone()
    assert row is not None
    assert row[0] and len(row[0]) > 0
    con.close()


def test_init_db_new_db_meta_has_prompt_version(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = get_connection(db_path)
    row = con.execute("SELECT value FROM meta WHERE key='prompt_version'").fetchone()
    assert row is not None
    assert len(row[0]) == 8
    con.close()


def test_migration_v2_converts_skipped(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with legacy schema (user_version=1, pre-v2)
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute("INSERT INTO conversations(id, source_path) VALUES ('conv1', '/src')")
    raw_con.execute(
        """CREATE TABLE exchanges (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            ply_start       INT NOT NULL,
            ply_end         INT NOT NULL,
            user_content    TEXT NOT NULL,
            agent_content   TEXT NOT NULL,
            distilled_at    TIMESTAMP
        )"""
    )
    raw_con.execute(
        "INSERT INTO exchanges VALUES ('ex1', 'conv1', 0, 1, 'user1', 'agent1', 'skipped')"
    )
    raw_con.execute(
        "INSERT INTO exchanges VALUES ('ex2', 'conv1', 1, 2, 'user2', 'agent2', '2026-01-01T00:00:00')"
    )
    raw_con.execute("PRAGMA user_version = 1")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v2 migration
    init_db(db_path)

    # Verify v2 migration: 'skipped' becomes distill_status='skipped' with NULL distilled_at
    con = sqlite3.connect(db_path)
    row1 = con.execute(
        "SELECT distill_status, distilled_at FROM exchanges WHERE id='ex1'"
    ).fetchone()
    assert row1[0] == "skipped"
    assert row1[1] is None

    # Verify timestamp row: distill_status='distilled' with distilled_at preserved
    row2 = con.execute(
        "SELECT distill_status, distilled_at FROM exchanges WHERE id='ex2'"
    ).fetchone()
    assert row2[0] == "distilled"
    assert row2[1] == "2026-01-01T00:00:00"
    con.close()


def test_migration_v3_creates_meta(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=2 (post-v2, pre-v3)
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute("INSERT INTO conversations(id, source_path) VALUES ('conv1', '/src')")
    raw_con.execute(
        """CREATE TABLE exchanges (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            ply_start       INT NOT NULL,
            ply_end         INT NOT NULL,
            user_content    TEXT NOT NULL,
            agent_content   TEXT NOT NULL,
            distilled_at    TIMESTAMP,
            distill_status  TEXT NOT NULL DEFAULT 'pending'
        )"""
    )
    raw_con.execute("PRAGMA user_version = 2")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v3 migration
    init_db(db_path)

    # Verify meta table exists with embedding_model and prompt_version
    con = sqlite3.connect(db_path)
    meta_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    assert meta_exists is not None

    embedding_model = con.execute(
        "SELECT value FROM meta WHERE key='embedding_model'"
    ).fetchone()
    assert embedding_model is not None

    prompt_version = con.execute(
        "SELECT value FROM meta WHERE key='prompt_version'"
    ).fetchone()
    assert prompt_version is not None
    con.close()


def test_migration_v4_creates_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=3 (post-v3, pre-v4)
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute("INSERT INTO conversations(id, source_path) VALUES ('conv1', '/src')")
    raw_con.execute(
        """CREATE TABLE exchanges (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            ply_start       INT NOT NULL,
            ply_end         INT NOT NULL,
            user_content    TEXT NOT NULL,
            agent_content   TEXT NOT NULL,
            distilled_at    TIMESTAMP,
            distill_status  TEXT NOT NULL DEFAULT 'pending'
        )"""
    )
    raw_con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    raw_con.execute(
        "INSERT INTO meta VALUES ('embedding_model', 'test-model')"
    )
    raw_con.execute(
        "INSERT INTO meta VALUES ('prompt_version', 'v0000001')"
    )
    raw_con.execute(
        """CREATE TABLE palace_objects (
            id               TEXT PRIMARY KEY,
            exchange_id      TEXT NOT NULL,
            exchange_core    TEXT NOT NULL,
            specific_context TEXT NOT NULL,
            distill_text     TEXT NOT NULL
        )"""
    )
    raw_con.execute(
        """CREATE TABLE rooms (
            id               TEXT PRIMARY KEY,
            palace_object_id TEXT NOT NULL,
            room_type        TEXT NOT NULL,
            room_key         TEXT NOT NULL,
            room_label       TEXT NOT NULL,
            relevance        REAL NOT NULL,
            dedup_hash       TEXT NOT NULL
        )"""
    )
    raw_con.execute(
        """CREATE TABLE symbols (
            id               TEXT PRIMARY KEY,
            palace_object_id TEXT NOT NULL,
            symbol_name      TEXT NOT NULL,
            symbol_kind      TEXT NOT NULL,
            file_path        TEXT NOT NULL,
            signature        TEXT NOT NULL,
            line             INT NOT NULL,
            dedup_hash       TEXT NOT NULL
        )"""
    )
    raw_con.execute("PRAGMA user_version = 3")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v4 migration
    init_db(db_path)

    # Verify indexes exist
    con = sqlite3.connect(db_path)
    indexes = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_rooms_palace_object_id', 'idx_symbols_palace_object_id', 'idx_palace_objects_exchange_id')"
    ).fetchall()
    index_names = [idx[0] for idx in indexes]
    assert "idx_rooms_palace_object_id" in index_names
    assert "idx_symbols_palace_object_id" in index_names
    assert "idx_palace_objects_exchange_id" in index_names
    con.close()


def test_init_db_idempotent_user_version_4(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    init_db(db_path)

    con = sqlite3.connect(db_path)
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == 4
    con.close()


def test_check_drift_no_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    drifts = check_drift(db_path)
    assert drifts == []


def test_check_drift_detects_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    # Modify meta to introduce drift
    con = get_connection(db_path)
    con.execute("UPDATE meta SET value='old_value' WHERE key='prompt_version'")
    con.commit()
    con.close()

    # Check drift should detect the mismatch
    drifts = check_drift(db_path)
    assert len(drifts) > 0
    drift_keys = [d[0] for d in drifts]
    assert "prompt_version" in drift_keys


def test_check_drift_absent_meta_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a legacy DB without meta table (pre-v3)
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute("INSERT INTO conversations(id, source_path) VALUES ('conv1', '/src')")
    raw_con.execute(
        """CREATE TABLE exchanges (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            ply_start       INT NOT NULL,
            ply_end         INT NOT NULL,
            user_content    TEXT NOT NULL,
            agent_content   TEXT NOT NULL,
            distilled_at    TIMESTAMP
        )"""
    )
    raw_con.execute("PRAGMA user_version = 1")
    raw_con.commit()
    raw_con.close()

    # check_drift should return [] without raising an exception
    drifts = check_drift(db_path)
    assert drifts == []


def test_init_db_chmod_600(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    mode_str = oct(db_path.stat().st_mode)[-3:]
    assert mode_str == "600"
