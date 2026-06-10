"""
SQLite DB の初期化・スキーマ定義・接続管理

テーブル構成:
  conversations  - .jsonl ファイル単位の会話記録（重複排除キャッシュ）
  exchanges      - exchange 単位の verbatim テキスト
  exchanges_fts  - exchanges の FTS5 仮想テーブル（BM25 verbatim 検索用）
  vec_exchanges  - sqlite-vec HNSW インデックス（Phase1 verbatim ベクトル検索用）
  palace_objects - 蒸留済み palace object（exchange_core + specific_context）
  rooms          - palace object の room_assignments
  vec_palace     - sqlite-vec HNSW インデックス（Phase2 distilled ベクトル検索用）
  symbols        - tree-sitter 解決済みシンボル（Phase3 コード逆引き用）
  _MIGRATIONS    - 逐次マイグレーション関数リスト（user_version ベース）
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

import sqlite_vec


def _migrate_v1_add_last_ply_end(con: sqlite3.Connection) -> None:
    """Migration v1: Add last_ply_end column to conversations table if absent."""
    columns = con.execute("PRAGMA table_info(conversations)").fetchall()
    column_names = [col[1] for col in columns]

    if "last_ply_end" not in column_names:
        con.execute(
            "ALTER TABLE conversations ADD COLUMN last_ply_end INT NOT NULL DEFAULT -1"
        )


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1_add_last_ply_end,
]


def _run_migrations(con: sqlite3.Connection) -> None:
    """Run pending migrations based on PRAGMA user_version."""
    current_version: int = con.execute("PRAGMA user_version").fetchone()[0]

    for target_version, fn in enumerate(_MIGRATIONS, start=1):
        if target_version > current_version:
            con.execute("BEGIN")
            try:
                fn(con)
                con.execute(f"PRAGMA user_version = {target_version}")
                con.execute("COMMIT")
            except Exception:
                con.rollback()
                raise


def get_connection(db_path: Path) -> sqlite3.Connection:
    """sqlite-vec 拡張をロードし WAL モード・busy_timeout を設定した接続を返す"""
    con = sqlite3.connect(db_path, timeout=10.0)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: Path) -> None:
    """DB を初期化してスキーマを作成する（冪等）"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = get_connection(db_path)

    # Check if conversations table exists (indicates existing DB)
    table_exists = con.execute(
        'SELECT name FROM sqlite_master WHERE type="table" AND name="conversations"'
    ).fetchone()

    if table_exists is None:
        # New DB: run core schema
        con.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id            TEXT PRIMARY KEY,   -- sha256(source_path)
                source_path   TEXT NOT NULL UNIQUE,
                started_at    TIMESTAMP,
                last_ply_end  INT  NOT NULL DEFAULT -1  -- 最後にインデックスした ply_end（差分用）
            );

            CREATE TABLE IF NOT EXISTS exchanges (
                id              TEXT PRIMARY KEY,  -- sha256(conversation_id + ":" + user_uuid)
                conversation_id TEXT NOT NULL,
                ply_start       INT  NOT NULL,
                ply_end         INT  NOT NULL,
                user_content    TEXT NOT NULL,
                agent_content   TEXT NOT NULL,
                distilled_at    TIMESTAMP          -- NULL = 未蒸留
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_fts USING fts5(
                user_content,
                agent_content,
                content=exchanges,
                content_rowid=rowid
            );

            CREATE TRIGGER IF NOT EXISTS exchanges_ai
            AFTER INSERT ON exchanges BEGIN
                INSERT INTO exchanges_fts(rowid, user_content, agent_content)
                VALUES (new.rowid, new.user_content, new.agent_content);
            END;

            CREATE TRIGGER IF NOT EXISTS exchanges_ad
            AFTER DELETE ON exchanges BEGIN
                INSERT INTO exchanges_fts(exchanges_fts, rowid, user_content, agent_content)
                VALUES ('delete', old.rowid, old.user_content, old.agent_content);
            END;

            CREATE TRIGGER IF NOT EXISTS exchanges_au
            AFTER UPDATE ON exchanges BEGIN
                INSERT INTO exchanges_fts(exchanges_fts, rowid, user_content, agent_content)
                VALUES ('delete', old.rowid, old.user_content, old.agent_content);
                INSERT INTO exchanges_fts(rowid, user_content, agent_content)
                VALUES (new.rowid, new.user_content, new.agent_content);
            END;

            CREATE TABLE IF NOT EXISTS palace_objects (
                id               TEXT PRIMARY KEY,
                exchange_id      TEXT NOT NULL,
                exchange_core    TEXT NOT NULL,
                specific_context TEXT NOT NULL,
                distill_text     TEXT NOT NULL    -- exchange_core + newline + specific_context
            );

            CREATE TABLE IF NOT EXISTS rooms (
                id               TEXT PRIMARY KEY,
                palace_object_id TEXT NOT NULL,
                room_type        TEXT NOT NULL,   -- "file" / "concept" / "workflow"
                room_key         TEXT NOT NULL,
                room_label       TEXT NOT NULL,
                relevance        REAL NOT NULL,
                dedup_hash       TEXT NOT NULL    -- hash(room_type, room_key)
            );

            CREATE TABLE IF NOT EXISTS symbols (
                id               TEXT PRIMARY KEY,   -- sha256(symbol_name + file_path)
                palace_object_id TEXT NOT NULL,
                symbol_name      TEXT NOT NULL,       -- "AuthMiddleware.validate"
                symbol_kind      TEXT NOT NULL,       -- "function" / "class" / "method"
                file_path        TEXT NOT NULL,
                signature        TEXT NOT NULL,
                line             INT  NOT NULL,
                dedup_hash       TEXT NOT NULL        -- sha256(symbol_name + file_path)
            );
        """)
        con.execute(f"PRAGMA user_version = {len(_MIGRATIONS)}")
    else:
        # Existing DB: run migrations
        _run_migrations(con)

    # sqlite-vec の仮想テーブル（HNSW, Phase1 verbatim embedding 用）
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_exchanges USING vec0(
            exchange_id TEXT PRIMARY KEY,
            embedding   FLOAT[384]
        )
    """)

    # sqlite-vec の仮想テーブル（HNSW, Phase2 distilled embedding 用）
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_palace USING vec0(
            palace_id TEXT PRIMARY KEY,
            embedding FLOAT[384]
        )
    """)

    con.commit()
    con.close()
