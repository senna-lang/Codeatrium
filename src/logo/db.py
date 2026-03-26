"""
SQLite DB の初期化・スキーマ定義・接続管理

テーブル構成:
  conversations  - .jsonl ファイル単位の会話記録（重複排除キャッシュ）
  exchanges      - exchange 単位の verbatim テキスト
  exchanges_fts  - exchanges の FTS5 仮想テーブル（BM25 verbatim 検索用）
  vec_exchanges  - sqlite-vec HNSW インデックス（Phase1 verbatim ベクトル検索用）
  palace_objects - 蒸留済み palace object（exchange_core + specific_context）
  palace_fts     - palace_objects の FTS5 仮想テーブル（将来用）
  rooms          - palace object の room_assignments
  vec_palace     - sqlite-vec HNSW インデックス（Phase2 distilled ベクトル検索用）
"""

import sqlite3
from pathlib import Path

import sqlite_vec


def get_connection(db_path: Path) -> sqlite3.Connection:
    """sqlite-vec 拡張をロードした接続を返す"""
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: Path) -> None:
    """DB を初期化してスキーマを作成する（冪等）"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = get_connection(db_path)

    con.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,   -- sha256(source_path)
            source_path TEXT NOT NULL UNIQUE,
            started_at  TIMESTAMP
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
            distill_text     TEXT NOT NULL,   -- exchange_core + newline + specific_context
            bm25_text        TEXT NOT NULL    -- FTS5 検索対象（SPEC Section 6 準拠の連結テキスト）
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS palace_fts USING fts5(
            bm25_text,
            content=palace_objects,
            content_rowid=rowid
        );

        CREATE TRIGGER IF NOT EXISTS palace_ai
        AFTER INSERT ON palace_objects BEGIN
            INSERT INTO palace_fts(rowid, bm25_text)
            VALUES (new.rowid, new.bm25_text);
        END;

        CREATE TABLE IF NOT EXISTS rooms (
            id               TEXT PRIMARY KEY,
            palace_object_id TEXT NOT NULL,
            room_type        TEXT NOT NULL,   -- "file" / "concept" / "workflow"
            room_key         TEXT NOT NULL,
            room_label       TEXT NOT NULL,
            relevance        REAL NOT NULL,
            dedup_hash       TEXT NOT NULL    -- hash(room_type, room_key)
        );
    """)

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
