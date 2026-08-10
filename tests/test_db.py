"""
DB 初期化・スキーマのテスト
"""

import hashlib
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
    assert user_version == len(_MIGRATIONS)
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


def test_migration_v5_creates_exchange_files(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=4 (post-v4, pre-v5)
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
    raw_con.execute("PRAGMA user_version = 4")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v5 migration
    init_db(db_path)

    # Verify exchange_files table exists
    con = sqlite3.connect(db_path)
    exchange_files_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='exchange_files'"
    ).fetchone()
    assert exchange_files_exists is not None

    # Verify exchange_files columns
    table_info = con.execute("PRAGMA table_info(exchange_files)").fetchall()
    column_names = [col[1] for col in table_info]
    assert "exchange_id" in column_names
    assert "file_path" in column_names
    con.close()


def test_migration_v6_recomputes_symbol_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=5 (so only v6 and v7 run)
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

    # Insert palace_objects row so v7 doesn't delete the symbol as orphan
    raw_con.execute(
        "INSERT INTO palace_objects VALUES ('po1', 'ex1', 'c', 's', 'c\ns')"
    )

    # Insert symbol with OLD id formula: sha256("Sym:file.py")
    old_id = hashlib.sha256(b"Sym:file.py").hexdigest()
    raw_con.execute(
        "INSERT INTO symbols VALUES (?, 'po1', 'Sym', 'function', 'file.py', 'def Sym', 1, ?)",
        (old_id, old_id),
    )

    raw_con.execute("PRAGMA user_version = 5")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v6 and v7 migrations
    init_db(db_path)

    # Verify symbol id was recomputed with NEW formula: sha256("Sym:file.py:po1")
    con = sqlite3.connect(db_path)
    expected_new_id = hashlib.sha256(b"Sym:file.py:po1").hexdigest()
    row = con.execute(
        "SELECT id FROM symbols WHERE symbol_name='Sym'"
    ).fetchone()
    assert row is not None
    assert row[0] == expected_new_id
    con.close()


def test_migration_v7_resets_orphan_distilled(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=6 (so only v7 runs)
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

    # Insert exchanges row with no palace_objects referencing it
    raw_con.execute(
        "INSERT INTO exchanges VALUES ('exX', 'conv1', 0, 1, 'user', 'agent', '2026-01-01', 'distilled')"
    )

    raw_con.execute("PRAGMA user_version = 6")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v7 migration
    init_db(db_path)

    # Verify exchanges row distill_status and distilled_at were reset
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT distill_status, distilled_at FROM exchanges WHERE id='exX'"
    ).fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] is None
    con.close()


def test_migration_v7_removes_orphan_symbols(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=6 (so only v7 runs)
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

    # Insert symbol with palace_object_id="ghost" but NO palace_objects row with that id
    ghost_id = hashlib.sha256(b"Ghost:test.py").hexdigest()
    raw_con.execute(
        "INSERT INTO symbols VALUES (?, 'ghost', 'Ghost', 'function', 'test.py', 'def Ghost', 1, ?)",
        (ghost_id, ghost_id),
    )

    raw_con.execute("PRAGMA user_version = 6")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v7 migration
    init_db(db_path)

    # Verify orphan symbol was deleted
    con = sqlite3.connect(db_path)
    count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    assert count == 0
    con.close()


def test_migration_v7_removes_bm25_text_column(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=6 (so only v7 runs)
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
            distill_text     TEXT NOT NULL,
            bm25_text        TEXT NOT NULL
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

    # Insert palace_objects row with bm25_text
    raw_con.execute(
        "INSERT INTO palace_objects VALUES ('po1', 'ex1', 'c', 's', 'c\ns', 'legacy')"
    )

    raw_con.execute("PRAGMA user_version = 6")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v7 migration
    init_db(db_path)

    # Verify bm25_text column was removed
    con = sqlite3.connect(db_path)
    table_info = con.execute("PRAGMA table_info(palace_objects)").fetchall()
    column_names = [col[1] for col in table_info]
    assert "bm25_text" not in column_names

    # Verify palace_objects row still exists
    row = con.execute(
        "SELECT id FROM palace_objects WHERE id='po1'"
    ).fetchone()
    assert row is not None
    con.close()


def test_migration_v8_adds_git_branch_column(tmp_path: Path) -> None:
    """Test that v8 migration adds git_branch column to exchanges table."""
    db_path = tmp_path / "memory.db"

    # Create a raw sqlite3 DB with user_version=7 (post-v7, pre-v8)
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
    raw_con.execute(
        "CREATE TABLE exchange_files (exchange_id TEXT, file_path TEXT, PRIMARY KEY(exchange_id, file_path))"
    )

    raw_con.execute("PRAGMA user_version = 7")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v8 migration
    init_db(db_path)

    # Verify git_branch column exists
    con = sqlite3.connect(db_path)
    table_info = con.execute("PRAGMA table_info(exchanges)").fetchall()
    column_names = [col[1] for col in table_info]
    assert "git_branch" in column_names
    con.close()


def test_migration_v8_backfills_git_branch(tmp_path: Path) -> None:
    """Test that v8 migration backfills git_branch from jsonl file."""
    import json

    db_path = tmp_path / "memory.db"
    jsonl_path = tmp_path / "session.jsonl"

    # Create a jsonl file with a user entry containing gitBranch
    user_entry = {
        "uuid": "user1",
        "gitBranch": "main",
        "type": "user",
        "content": "This is a long user content string that should be stored in the database",
    }
    agent_entry = {
        "type": "assistant",
        "content": "This is a long agent response string that should be stored in the database",
    }
    jsonl_path.write_text(json.dumps(user_entry) + "\n" + json.dumps(agent_entry) + "\n")

    # Create a v7 DB with a conversation pointing to the jsonl file
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute(
        "INSERT INTO conversations(id, source_path) VALUES ('conv1', ?)",
        (str(jsonl_path),),
    )
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
    # Insert an exchange at ply_start=0 (matching the user entry in jsonl)
    raw_con.execute(
        "INSERT INTO exchanges VALUES ('ex1', 'conv1', 0, 1, 'user', 'agent', NULL, 'pending')"
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
    raw_con.execute(
        "CREATE TABLE exchange_files (exchange_id TEXT, file_path TEXT, PRIMARY KEY(exchange_id, file_path))"
    )

    raw_con.execute("PRAGMA user_version = 7")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v8 migration and backfill git_branch
    init_db(db_path)

    # Verify git_branch was backfilled to 'main'
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT git_branch FROM exchanges WHERE id='ex1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "main"
    con.close()


def test_migration_v8_missing_jsonl_stays_null(tmp_path: Path) -> None:
    """Test that v8 migration handles missing jsonl file gracefully, leaving git_branch NULL."""
    db_path = tmp_path / "memory.db"

    # Create a v7 DB with a conversation pointing to a non-existent jsonl file
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute(
        "INSERT INTO conversations(id, source_path) VALUES ('conv1', '/nonexistent/path.jsonl')"
    )
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
    # Insert an exchange at ply_start=0
    raw_con.execute(
        "INSERT INTO exchanges VALUES ('ex1', 'conv1', 0, 1, 'user', 'agent', NULL, 'pending')"
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
    raw_con.execute(
        "CREATE TABLE exchange_files (exchange_id TEXT, file_path TEXT, PRIMARY KEY(exchange_id, file_path))"
    )

    raw_con.execute("PRAGMA user_version = 7")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v8 migration without raising an error
    init_db(db_path)

    # Verify git_branch is NULL (not filled in due to missing jsonl)
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT git_branch FROM exchanges WHERE id='ex1'"
    ).fetchone()
    assert row is not None
    assert row[0] is None
    con.close()


def test_migration_v8_malformed_line_coordinate(tmp_path: Path) -> None:
    """Test that malformed jsonl lines do NOT shift ply coordinates during backfill.

    Verifies that when a malformed JSON line is at index 0 and a valid gitBranch
    entry is at index 1, the exchange with ply_start=1 gets the correct gitBranch,
    proving the malformed line did not consume a ply slot.
    """
    import json

    db_path = tmp_path / "memory.db"
    jsonl_path = tmp_path / "session.jsonl"

    # Create a jsonl file with:
    # Line 0: malformed JSON (not valid JSON)
    # Line 1: valid JSON user entry with gitBranch='feature-x'
    malformed_line = "not-json\n"
    user_entry = {
        "uuid": "user1",
        "gitBranch": "feature-x",
        "type": "human",
        "content": "This is a long user content string that is valid for the database",
    }
    jsonl_path.write_text(malformed_line + json.dumps(user_entry) + "\n")

    # Create a v7 DB with a conversation pointing to the jsonl file
    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute(
        "INSERT INTO conversations(id, source_path) VALUES ('conv1', ?)",
        (str(jsonl_path),),
    )
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
    # Insert an exchange at ply_start=0 (after the malformed line)
    raw_con.execute(
        "INSERT INTO exchanges VALUES ('ex1', 'conv1', 0, 1, 'user', 'agent', NULL, 'pending')"
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
    raw_con.execute(
        "CREATE TABLE exchange_files (exchange_id TEXT, file_path TEXT, PRIMARY KEY(exchange_id, file_path))"
    )

    raw_con.execute("PRAGMA user_version = 7")
    raw_con.commit()
    raw_con.close()

    # Run init_db which should run v8 migration and backfill git_branch
    init_db(db_path)

    # Verify git_branch was backfilled to 'feature-x' (not NULL)
    # This proves ply_index was 1 when the valid line was processed
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT git_branch FROM exchanges WHERE id='ex1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "feature-x"
    con.close()


def test_migration_v8_idempotent(tmp_path: Path) -> None:
    """Test that v8 migration is idempotent (can be run multiple times)."""
    db_path = tmp_path / "memory.db"

    # Initialize a fresh DB (which runs v8)
    init_db(db_path)

    # Check user_version after first init
    con = sqlite3.connect(db_path)
    user_version_1 = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()

    # Run init_db again (should be idempotent)
    init_db(db_path)

    # Check user_version after second init
    con = sqlite3.connect(db_path)
    user_version_2 = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()

    # Verify user_version equals the number of migrations both times
    assert user_version_1 == len(_MIGRATIONS)
    assert user_version_2 == len(_MIGRATIONS)


def test_init_db_new_db_has_code_touches_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='code_touches'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_new_db_has_code_symbols_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='code_symbols'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_new_db_has_code_edges_table(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='code_edges'"
    )
    assert cur.fetchone() is not None
    con.close()


def test_init_db_new_db_conversations_has_parent_session_ref_column(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = sqlite3.connect(db_path)
    columns = con.execute("PRAGMA table_info(conversations)").fetchall()
    column_names = [col[1] for col in columns]
    assert "parent_session_ref" in column_names
    con.close()


def test_migration_v9_adds_code_touches_tables(tmp_path: Path) -> None:
    """Test that v9 migration creates code_touches/code_symbols/code_edges on a pre-v9 DB."""
    db_path = tmp_path / "memory.db"

    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute("PRAGMA user_version = 8")
    raw_con.commit()
    raw_con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('code_touches', 'code_symbols', 'code_edges')"
        ).fetchall()
    }
    assert tables == {"code_touches", "code_symbols", "code_edges"}
    con.close()


def test_migration_v9_adds_parent_session_ref_column(tmp_path: Path) -> None:
    """Test that v9 migration adds parent_session_ref to an existing conversations table."""
    db_path = tmp_path / "memory.db"

    raw_con = sqlite3.connect(db_path)
    raw_con.execute(
        """CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            last_ply_end INT NOT NULL DEFAULT -1
        )"""
    )
    raw_con.execute(
        "INSERT INTO conversations(id, source_path) VALUES ('conv1', '/src')"
    )
    raw_con.execute("PRAGMA user_version = 8")
    raw_con.commit()
    raw_con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    columns = con.execute("PRAGMA table_info(conversations)").fetchall()
    column_names = [col[1] for col in columns]
    assert "parent_session_ref" in column_names
    # 既存行が壊れていないことも確認する
    row = con.execute("SELECT id FROM conversations WHERE id='conv1'").fetchone()
    assert row is not None
    con.close()


def test_migration_v9_idempotent(tmp_path: Path) -> None:
    """Test that v9 migration is idempotent (can be run multiple times)."""
    db_path = tmp_path / "memory.db"

    init_db(db_path)
    con = sqlite3.connect(db_path)
    user_version_1 = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()

    init_db(db_path)
    con = sqlite3.connect(db_path)
    user_version_2 = con.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('code_touches', 'code_symbols', 'code_edges')"
        ).fetchall()
    }
    con.close()

    assert user_version_1 == len(_MIGRATIONS)
    assert user_version_2 == len(_MIGRATIONS)
    assert tables == {"code_touches", "code_symbols", "code_edges"}


# ---- _backfill_legacy_code_edges（design §7 段階B・C） ----
#
# 実際のタイミングを再現する: この機能は「既に exchange_files/symbols が
# 何ヶ月も蓄積した後」に配備される。init_db を2回呼ぶ（1回目でスキーマだけ
# 作りフラグが0件のまま立ってしまう）のではなく、v9 相当のスキーマと
# 過去データを raw sqlite で先に作ってから、init_db を1回だけ呼ぶ。


def _project_db_path(tmp_path: Path) -> Path:
    """project_root / .codeatrium / memory.db という実際のレイアウトを再現する"""
    project_root = tmp_path / "proj"
    (project_root / ".codeatrium").mkdir(parents=True)
    return project_root / ".codeatrium" / "memory.db"


def _build_pre_backfill_db(db_path: Path) -> sqlite3.Connection:
    """v9 到達済み・legacy_edges_backfilled 未実行という現実的な前提状態を作る"""
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, source_path TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP, last_ply_end INT NOT NULL DEFAULT -1,
            parent_session_ref TEXT
        );
        CREATE TABLE exchanges (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            ply_start INT NOT NULL, ply_end INT NOT NULL,
            user_content TEXT NOT NULL, agent_content TEXT NOT NULL,
            distilled_at TIMESTAMP, distill_status TEXT NOT NULL DEFAULT 'pending',
            git_branch TEXT
        );
        CREATE TABLE palace_objects (
            id TEXT PRIMARY KEY, exchange_id TEXT NOT NULL,
            exchange_core TEXT NOT NULL, specific_context TEXT NOT NULL, distill_text TEXT NOT NULL
        );
        CREATE TABLE symbols (
            id TEXT PRIMARY KEY, palace_object_id TEXT NOT NULL, symbol_name TEXT NOT NULL,
            symbol_kind TEXT NOT NULL, file_path TEXT NOT NULL, signature TEXT NOT NULL,
            line INT NOT NULL, dedup_hash TEXT NOT NULL
        );
        CREATE TABLE exchange_files (
            exchange_id TEXT, file_path TEXT, PRIMARY KEY (exchange_id, file_path)
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE code_touches (
            id TEXT PRIMARY KEY, exchange_id TEXT NOT NULL, harness TEXT NOT NULL,
            tool_call_id TEXT NOT NULL, file_path TEXT NOT NULL, touch_kind TEXT NOT NULL,
            locator_kind TEXT NOT NULL, old_start INT, old_lines INT, new_start INT, new_lines INT,
            old_string TEXT, new_string TEXT, symbol_name TEXT, resolved_by TEXT,
            added INT NOT NULL DEFAULT 0, removed INT NOT NULL DEFAULT 0, ts TEXT
        );
        CREATE TABLE code_symbols (
            id TEXT PRIMARY KEY, file_path TEXT NOT NULL, symbol_name TEXT NOT NULL,
            symbol_kind TEXT NOT NULL, signature TEXT NOT NULL, line INT NOT NULL,
            end_line INT NOT NULL, lang TEXT NOT NULL, resolved_at TEXT NOT NULL,
            UNIQUE(file_path, symbol_name)
        );
        CREATE TABLE code_edges (
            id TEXT PRIMARY KEY, exchange_id TEXT NOT NULL, file_path TEXT NOT NULL,
            symbol_id TEXT, edge_kind TEXT NOT NULL, granularity TEXT NOT NULL,
            confidence REAL NOT NULL, added INT NOT NULL DEFAULT 0, ts TEXT
        );
        PRAGMA user_version = 9;
    """)
    return con


def _seed_conversation_and_exchange(con: sqlite3.Connection, conv_id: str, ex_id: str) -> None:
    con.execute(
        "INSERT INTO conversations (id, source_path) VALUES (?, ?)",
        (conv_id, f"/fake/{conv_id}.jsonl"),
    )
    con.execute(
        """INSERT INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content)
           VALUES (?, ?, 0, 1, 'u', 'a')""",
        (ex_id, conv_id),
    )


def test_backfill_stage_b_creates_file_edge_from_exchange_files(tmp_path: Path) -> None:
    db_path = _project_db_path(tmp_path)
    project_root = db_path.parent.parent

    con = _build_pre_backfill_db(db_path)
    _seed_conversation_and_exchange(con, "c1", "ex1")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        ("ex1", str(project_root / "src" / "foo.py")),
    )
    con.commit()
    con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT file_path, symbol_id, edge_kind, granularity, confidence, ts FROM code_edges WHERE exchange_id='ex1'"
    ).fetchone()
    con.close()

    assert row is not None
    assert row[0] == "src/foo.py"  # project_root からの相対パスへ正規化
    assert row[1] is None
    assert row[2] == "mention"
    assert row[3] == "file"
    assert row[4] == 0.5
    assert row[5] is None


def test_backfill_stage_b_skips_paths_outside_project_root(tmp_path: Path) -> None:
    db_path = _project_db_path(tmp_path)

    con = _build_pre_backfill_db(db_path)
    _seed_conversation_and_exchange(con, "c1", "ex1")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        ("ex1", "/tmp/scratch/other-project/foo.py"),
    )
    con.commit()
    con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    row = con.execute("SELECT 1 FROM code_edges WHERE exchange_id='ex1'").fetchone()
    con.close()

    assert row is None


def test_backfill_stage_b_skips_exchange_already_covered_by_code_edges(tmp_path: Path) -> None:
    """既に code_edges を持つ exchange（新パイプライン経由）は上書き・重複させない"""
    db_path = _project_db_path(tmp_path)
    project_root = db_path.parent.parent

    con = _build_pre_backfill_db(db_path)
    _seed_conversation_and_exchange(con, "c1", "ex1")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        ("ex1", str(project_root / "src" / "foo.py")),
    )
    con.execute(
        """INSERT INTO code_edges
           (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts)
           VALUES ('real-edge', 'ex1', 'src/foo.py', NULL, 'edit', 'line', 1.0, 3, '2026-08-09T00:00:00Z')"""
    )
    con.commit()
    con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT id FROM code_edges WHERE exchange_id='ex1'").fetchall()
    con.close()

    assert [r[0] for r in rows] == ["real-edge"]


def test_backfill_stage_c_creates_file_edge_from_legacy_symbols(tmp_path: Path) -> None:
    db_path = _project_db_path(tmp_path)

    con = _build_pre_backfill_db(db_path)
    _seed_conversation_and_exchange(con, "c1", "ex1")
    con.execute(
        """INSERT INTO palace_objects (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES ('p1', 'ex1', 'core', 'ctx', 'core' || char(10) || 'ctx')"""
    )
    con.execute(
        """INSERT INTO symbols (id, palace_object_id, symbol_name, symbol_kind, file_path, signature, line, dedup_hash)
           VALUES ('sym1', 'p1', 'greet', 'function', 'src/legacy.py', 'def greet():', 3, 'hash1')"""
    )
    con.commit()
    con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT file_path, edge_kind, granularity, confidence FROM code_edges WHERE exchange_id='ex1'"
    ).fetchone()
    con.close()

    assert row == ("src/legacy.py", "mention", "file", 0.5)


def test_backfill_stage_c_skips_exchange_already_covered_by_stage_b(tmp_path: Path) -> None:
    """段階Bが先に処理される。同じexchangeなら段階Cは(別ファイルでも)追加しない"""
    db_path = _project_db_path(tmp_path)
    project_root = db_path.parent.parent

    con = _build_pre_backfill_db(db_path)
    _seed_conversation_and_exchange(con, "c1", "ex1")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        ("ex1", str(project_root / "src" / "foo.py")),
    )
    con.execute(
        """INSERT INTO palace_objects (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES ('p1', 'ex1', 'core', 'ctx', 'core' || char(10) || 'ctx')"""
    )
    con.execute(
        """INSERT INTO symbols (id, palace_object_id, symbol_name, symbol_kind, file_path, signature, line, dedup_hash)
           VALUES ('sym1', 'p1', 'greet', 'function', 'src/legacy.py', 'def greet():', 3, 'hash1')"""
    )
    con.commit()
    con.close()

    init_db(db_path)

    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT file_path FROM code_edges WHERE exchange_id='ex1'").fetchall()
    con.close()

    assert [r[0] for r in rows] == ["src/foo.py"]


def test_backfill_sets_meta_flag_so_later_init_db_calls_do_not_rescan(tmp_path: Path) -> None:
    """meta フラグで一度だけ実行される——以後の init_db（毎回の loci index）は再走査しない"""
    db_path = _project_db_path(tmp_path)
    project_root = db_path.parent.parent

    con = _build_pre_backfill_db(db_path)
    _seed_conversation_and_exchange(con, "c1", "ex1")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        ("ex1", str(project_root / "src" / "foo.py")),
    )
    con.commit()
    con.close()

    init_db(db_path)  # 1回目: 実データをbackfillし、フラグを立てる

    con = get_connection(db_path)
    flag = con.execute(
        "SELECT 1 FROM meta WHERE key='legacy_edges_backfilled'"
    ).fetchone()
    assert flag is not None
    # フラグが立った後に、本来なら段階Bの対象になるはずの行を追加する
    _seed_conversation_and_exchange(con, "c2", "ex2")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
        ("ex2", str(project_root / "src" / "late.py")),
    )
    con.commit()
    con.close()

    init_db(db_path)  # 2回目（loci index が毎回呼ぶのを模す）: 再走査しないはず

    con = sqlite3.connect(db_path)
    row = con.execute("SELECT 1 FROM code_edges WHERE exchange_id='ex2'").fetchone()
    con.close()

    assert row is None
