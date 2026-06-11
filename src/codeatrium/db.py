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

import hashlib
import os
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


def _migrate_v2_add_distill_status(con: sqlite3.Connection) -> None:
    """Migration v2: exchanges に distill_status カラムを追加し既存データを変換する"""
    table_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges'").fetchone()
    if table_exists is None:
        return
    columns = con.execute("PRAGMA table_info(exchanges)").fetchall()
    column_names = [col[1] for col in columns]

    if "distill_status" not in column_names:
        con.execute(
            "ALTER TABLE exchanges ADD COLUMN distill_status TEXT NOT NULL DEFAULT 'pending'"
        )

    con.execute(
        "UPDATE exchanges SET distill_status='skipped', distilled_at=NULL WHERE distilled_at='skipped'"
    )
    con.execute(
        "UPDATE exchanges SET distill_status='distilled' WHERE distilled_at IS NOT NULL AND distilled_at != 'skipped'"
    )


def _migrate_v3_add_meta(con: sqlite3.Connection) -> None:
    """Migration v3: meta テーブルを新設し embedding_model と prompt_version を初期化する"""
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )

    from codeatrium.embedder import MODEL_NAME
    from codeatrium.llm import DISTILL_PROMPT_VERSION

    con.execute(
        "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
        ("embedding_model", MODEL_NAME),
    )
    con.execute(
        "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
        ("prompt_version", DISTILL_PROMPT_VERSION),
    )


def _migrate_v4_add_indexes(con: sqlite3.Connection) -> None:
    """Migration v4: rooms/symbols/palace_objects に検索用インデックスを追加する"""
    rooms_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rooms'").fetchone()
    if rooms_exists is not None:
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_rooms_palace_object_id ON rooms(palace_object_id)"
        )

    symbols_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='symbols'").fetchone()
    if symbols_exists is not None:
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbols_palace_object_id ON symbols(palace_object_id)"
        )

    palace_objects_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='palace_objects'").fetchone()
    if palace_objects_exists is not None:
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_palace_objects_exchange_id ON palace_objects(exchange_id)"
        )


def _migrate_v5_add_exchange_files(con: sqlite3.Connection) -> None:
    """Migration v5: exchange_files テーブルを新設する"""
    table_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exchange_files'").fetchone()
    if table_exists is None:
        con.execute(
            "CREATE TABLE exchange_files (exchange_id TEXT, file_path TEXT, PRIMARY KEY(exchange_id, file_path))"
        )


def _migrate_v6_recompute_symbol_ids(con: sqlite3.Connection) -> None:
    """Migration v6: symbols テーブルの id カラムを hash 再計算する"""
    symbols_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='symbols'").fetchone()
    if symbols_exists is None:
        return

    rows = con.execute("SELECT rowid, symbol_name, file_path, palace_object_id FROM symbols").fetchall()
    for rowid, symbol_name, file_path, palace_object_id in rows:
        new_id = hashlib.sha256((symbol_name + ":" + file_path + ":" + palace_object_id).encode()).hexdigest()
        con.execute("UPDATE symbols SET id=? WHERE rowid=?", (new_id, rowid))


def _migrate_v7_repair_distill(con: sqlite3.Connection) -> None:
    """Migration v7: palace_objects テーブルから bm25_text を削除・distill ステータス修復・orphan クリーンアップ"""
    # STEP1: bm25_text カラム削除
    palace_objects_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='palace_objects'").fetchone()
    if palace_objects_exists is not None:
        columns = con.execute("PRAGMA table_info(palace_objects)").fetchall()
        column_names = [col[1] for col in columns]
        if "bm25_text" in column_names:
            con.execute(
                "CREATE TABLE palace_objects_new (id TEXT PRIMARY KEY, exchange_id TEXT NOT NULL, exchange_core TEXT NOT NULL, specific_context TEXT NOT NULL, distill_text TEXT NOT NULL)"
            )
            con.execute(
                "INSERT INTO palace_objects_new (id, exchange_id, exchange_core, specific_context, distill_text) SELECT id, exchange_id, exchange_core, specific_context, distill_text FROM palace_objects"
            )
            con.execute("DROP TABLE palace_objects")
            con.execute("ALTER TABLE palace_objects_new RENAME TO palace_objects")
            con.execute("CREATE INDEX IF NOT EXISTS idx_palace_objects_exchange_id ON palace_objects(exchange_id)")

    # STEP2: re-distill reset
    exchanges_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges'").fetchone()
    palace_objects_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='palace_objects'").fetchone()
    if exchanges_exists is not None and palace_objects_exists is not None:
        con.execute(
            "UPDATE exchanges SET distill_status='pending', distilled_at=NULL WHERE distill_status='distilled' AND id NOT IN (SELECT exchange_id FROM palace_objects)"
        )

    # STEP3: orphan cleanup
    rooms_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rooms'").fetchone()
    if rooms_exists is not None:
        con.execute(
            "DELETE FROM rooms WHERE palace_object_id NOT IN (SELECT id FROM palace_objects)"
        )

    symbols_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='symbols'").fetchone()
    if symbols_exists is not None:
        con.execute(
            "DELETE FROM symbols WHERE palace_object_id NOT IN (SELECT id FROM palace_objects)"
        )

    vec_palace_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_palace'").fetchone()
    if vec_palace_exists is not None:
        con.execute(
            "DELETE FROM vec_palace WHERE palace_id NOT IN (SELECT id FROM palace_objects)"
        )


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1_add_last_ply_end,
    _migrate_v2_add_distill_status,
    _migrate_v3_add_meta,
    _migrate_v4_add_indexes,
    _migrate_v5_add_exchange_files,
    _migrate_v6_recompute_symbol_ids,
    _migrate_v7_repair_distill,
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
    # memory.db 本体と WAL サイドカー（-wal / -shm）は会話・コードの逐語データを含むため
    # 所有者のみ読み書き可（0o600）にする。WAL モードでサイドカーが生成される。
    os.chmod(db_path, 0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.parent / (db_path.name + suffix)
        if sidecar.exists():
            os.chmod(sidecar, 0o600)

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
                distilled_at    TIMESTAMP,         -- NULL = 未蒸留
                distill_status  TEXT NOT NULL DEFAULT 'pending'
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

            CREATE TABLE IF NOT EXISTS exchange_files (
                exchange_id TEXT,
                file_path   TEXT,
                PRIMARY KEY (exchange_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

            CREATE INDEX IF NOT EXISTS idx_rooms_palace_object_id ON rooms(palace_object_id);
            CREATE INDEX IF NOT EXISTS idx_symbols_palace_object_id ON symbols(palace_object_id);
            CREATE INDEX IF NOT EXISTS idx_palace_objects_exchange_id ON palace_objects(exchange_id);
        """)

        from codeatrium.embedder import MODEL_NAME
        from codeatrium.llm import DISTILL_PROMPT_VERSION

        con.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
            ("embedding_model", MODEL_NAME),
        )
        con.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
            ("prompt_version", DISTILL_PROMPT_VERSION),
        )

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


def check_drift(db_path: Path) -> list[tuple[str, str, str]]:
    """meta テーブルの記録値と現行値を比較し不一致の (key, recorded, current) タプルリストを返す"""
    con = get_connection(db_path)
    try:
        # Check if meta table exists
        meta_exists = con.execute(
            'SELECT name FROM sqlite_master WHERE type="table" AND name="meta"'
        ).fetchone()

        if meta_exists is None:
            return []

        from codeatrium.embedder import MODEL_NAME
        from codeatrium.llm import DISTILL_PROMPT_VERSION

        # Get recorded values from meta table
        meta_rows = con.execute(
            "SELECT key, value FROM meta WHERE key IN ('embedding_model', 'prompt_version')"
        ).fetchall()
        recorded = {row[0]: row[1] for row in meta_rows}

        # Compare with current values
        drifts: list[tuple[str, str, str]] = []

        if "embedding_model" in recorded and recorded["embedding_model"] != MODEL_NAME:
            drifts.append(("embedding_model", recorded["embedding_model"], MODEL_NAME))

        if "prompt_version" in recorded and recorded["prompt_version"] != DISTILL_PROMPT_VERSION:
            drifts.append(("prompt_version", recorded["prompt_version"], DISTILL_PROMPT_VERSION))

        return drifts
    finally:
        con.close()
