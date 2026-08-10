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
  symbols        - tree-sitter 解決済みシンボル（Phase3 コード逆引き用、読み取り専用で残置）
  code_touches   - ハーネスの編集ログから記録時に保存する加工前の手がかり（design §4.1）
  code_symbols   - tree-sitter で解決したシンボルの正本（design §4.1）
  code_edges     - 会話とコードのひも付け（design §4.1）
  file_renames   - ファイル改名の記録（design §8.2、旧パス→新パス。問い合わせ時に逆向きにたどる）
  _MIGRATIONS    - 逐次マイグレーション関数リスト（user_version ベース）

`_backfill_legacy_code_edges` は _MIGRATIONS に含めない別経路（design §7 段階B・C）。
project_root（ファイルシステムのレイアウト）に依存する点が、DB だけに閉じた
他の migration と性質が違うため、`meta` テーブルの完了フラグで一度だけ実行する。
"""

import hashlib
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
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


def _migrate_v8_add_git_branch(con: sqlite3.Connection) -> None:
    """Migration v8: exchanges に git_branch カラムを追加し既存 exchange を jsonl 再パースでバックフィルする"""
    import json

    # Guard: check if exchanges table exists
    exchanges_table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges'").fetchone()
    if exchanges_table is None:
        return

    # CHECK if git_branch column already exists
    columns = con.execute("PRAGMA table_info(exchanges)").fetchall()
    column_names = [col[1] for col in columns]

    if "git_branch" not in column_names:
        con.execute("ALTER TABLE exchanges ADD COLUMN git_branch TEXT")

    # Nested function to extract git_branch from ply targets in a jsonl file
    def _extract_git_branch_from_ply(jsonl_path_str: str, ply_targets: list[int]) -> dict[int, str | None]:
        """
        Open jsonl file, iterate lines counting only successful json.loads.
        For each ply index in ply_targets, look for entry.get('gitBranch').
        Return mapping ply_index -> gitBranch (None if missing or empty string).
        """
        result: dict[int, str | None] = {}
        try:
            with open(jsonl_path_str, encoding='utf-8') as f:
                ply_index = 0
                for line in f:
                    try:
                        entry = json.loads(line)
                        if ply_index in ply_targets:
                            git_branch_raw = entry.get('gitBranch', '')
                            git_branch = git_branch_raw if isinstance(git_branch_raw, str) and git_branch_raw.strip() else None
                            result[ply_index] = git_branch
                        ply_index += 1
                    except json.JSONDecodeError:
                        # Skip malformed lines without incrementing ply_index
                        pass
        except Exception:
            # If file cannot be read, silently return empty mapping
            pass

        return result

    # QUERY existing exchanges with conversation info
    exchanges_exist = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges'").fetchone()
    conversations_exist = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()

    if exchanges_exist is None or conversations_exist is None:
        return

    rows = con.execute(
        "SELECT e.id, e.conversation_id, e.ply_start, c.source_path FROM exchanges e JOIN conversations c ON c.id = e.conversation_id"
    ).fetchall()

    # GROUP by source_path and backfill
    by_path: dict[str, list[tuple[str, int]]] = {}
    for ex_id, conv_id, ply_start, source_path in rows:
        if source_path not in by_path:
            by_path[source_path] = []
        by_path[source_path].append((ex_id, ply_start))

    for source_path, exchanges_for_path in by_path.items():
        ply_targets = [ply_start for _, ply_start in exchanges_for_path]
        branch_map = _extract_git_branch_from_ply(source_path, ply_targets)

        for ex_id, ply_start in exchanges_for_path:
            try:
                branch_value = branch_map.get(ply_start)
                con.execute(
                    "UPDATE exchanges SET git_branch = ? WHERE id = ?",
                    (branch_value, ex_id),
                )
            except Exception:
                # Silently continue on any exception so migration never aborts
                pass


def _migrate_v9_add_code_touches(con: sqlite3.Connection) -> None:
    """Migration v9: code_touches/code_symbols/code_edges を新設し conversations.parent_session_ref を追加する（design §4.1・§4.2）"""
    conversations_exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
    if conversations_exists is not None:
        columns = con.execute("PRAGMA table_info(conversations)").fetchall()
        column_names = [col[1] for col in columns]
        if "parent_session_ref" not in column_names:
            con.execute("ALTER TABLE conversations ADD COLUMN parent_session_ref TEXT")

    # executescript() は暗黙に COMMIT するため、_run_migrations の外側トランザクションが
    # 途中で閉じてしまう。個別の execute() に分けて同一トランザクション内に収める。
    con.execute("""
        CREATE TABLE IF NOT EXISTS code_touches (
            id           TEXT PRIMARY KEY,
            exchange_id  TEXT NOT NULL,
            harness      TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            file_path    TEXT NOT NULL,
            touch_kind   TEXT NOT NULL,

            locator_kind TEXT NOT NULL,
            old_start    INT,
            old_lines    INT,
            new_start    INT,
            new_lines    INT,
            old_string   TEXT,
            new_string   TEXT,

            symbol_name  TEXT,
            resolved_by  TEXT,
            added        INT NOT NULL DEFAULT 0,
            removed      INT NOT NULL DEFAULT 0,
            ts           TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_touches_exchange ON code_touches(exchange_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_touches_file     ON code_touches(file_path)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_touches_symbol   ON code_touches(file_path, symbol_name)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS code_symbols (
            id          TEXT PRIMARY KEY,
            file_path   TEXT NOT NULL,
            symbol_name TEXT NOT NULL,
            symbol_kind TEXT NOT NULL,
            signature   TEXT NOT NULL,
            line        INT NOT NULL,
            end_line    INT NOT NULL,
            lang        TEXT NOT NULL,
            resolved_at TEXT NOT NULL,
            UNIQUE(file_path, symbol_name)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(symbol_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_symbols_file ON code_symbols(file_path)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS code_edges (
            id          TEXT PRIMARY KEY,
            exchange_id TEXT NOT NULL,
            file_path   TEXT NOT NULL,
            symbol_id   TEXT,
            edge_kind   TEXT NOT NULL,
            granularity TEXT NOT NULL,
            confidence  REAL NOT NULL,
            added       INT NOT NULL DEFAULT 0,
            ts          TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_edges_symbol   ON code_edges(symbol_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_edges_file     ON code_edges(file_path)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_edges_exchange ON code_edges(exchange_id)")


def _migrate_v10_add_file_renames(con: sqlite3.Connection) -> None:
    """Migration v10: file_renames を新設する（design §8.2、ファイル改名の追従）"""
    con.execute("""
        CREATE TABLE IF NOT EXISTS file_renames (
            old_path TEXT NOT NULL,
            new_path TEXT NOT NULL,
            source   TEXT NOT NULL,
            ts       TEXT,
            PRIMARY KEY (old_path, new_path)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_file_renames_new_path ON file_renames(new_path)")


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1_add_last_ply_end,
    _migrate_v2_add_distill_status,
    _migrate_v3_add_meta,
    _migrate_v4_add_indexes,
    _migrate_v5_add_exchange_files,
    _migrate_v6_recompute_symbol_ids,
    _migrate_v7_repair_distill,
    _migrate_v8_add_git_branch,
    _migrate_v9_add_code_touches,
    _migrate_v10_add_file_renames,
]


def _backfill_legacy_code_edges(con: sqlite3.Connection, project_root: Path) -> None:
    """design §7 段階B・C: exchange_files・旧 symbols から、まだ code_edges を
    持たない exchange へファイル粒度の code_edges を作る。LLM は呼ばず、
    ディスクも読まない（DB内で完結）。`meta` の完了フラグで一度だけ実行する
    （project_root に依存するため `_MIGRATIONS`/user_version には含めない——
    他の migration は DB だけに閉じた操作であり、性質が異なる）。

    段階B（exchange_files）を先に処理し、段階C（旧 symbols）は
    段階Bで既にひも付いた exchange をスキップする（exchange_files の方が
    直接的な手がかりであり、蒸留経由の symbols より優先する）。
    project_root 外のパスは記録しない（不変条件3、normalize_repo_path と同じ判定）。
    """
    # meta は v3 で全既存DBに作られるはずだが、念のため（他の migration 関数と同じ流儀）
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    done = con.execute(
        "SELECT 1 FROM meta WHERE key = 'legacy_edges_backfilled'"
    ).fetchone()
    if done is not None:
        return

    from codeatrium.code_touches import FILE_CONFIDENCE, normalize_repo_path
    from codeatrium.utils import sha256

    def _insert_mention_edge(exchange_id: str, file_path: str) -> None:
        rel_path = (
            normalize_repo_path(file_path, str(project_root))
            if file_path.startswith("/")
            else file_path
        )
        if rel_path is None:
            return
        edge_id = sha256(f"{exchange_id}:{rel_path}::mention")
        con.execute(
            """INSERT OR IGNORE INTO code_edges
               (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts)
               VALUES (?, ?, ?, NULL, 'mention', 'file', ?, 0, NULL)""",
            (edge_id, exchange_id, rel_path, FILE_CONFIDENCE),
        )

    exchange_files_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='exchange_files'"
    ).fetchone()
    if exchange_files_exists is not None:
        rows = con.execute(
            """
            SELECT DISTINCT exchange_id, file_path FROM exchange_files
            WHERE exchange_id NOT IN (SELECT DISTINCT exchange_id FROM code_edges)
            """
        ).fetchall()
        for exchange_id, file_path in rows:
            _insert_mention_edge(exchange_id, file_path)

    symbols_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols'"
    ).fetchone()
    palace_objects_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='palace_objects'"
    ).fetchone()
    if symbols_exists is not None and palace_objects_exists is not None:
        rows = con.execute(
            """
            SELECT DISTINCT p.exchange_id, s.file_path
            FROM symbols s
            JOIN palace_objects p ON p.id = s.palace_object_id
            WHERE p.exchange_id NOT IN (SELECT DISTINCT exchange_id FROM code_edges)
            """
        ).fetchall()
        for exchange_id, file_path in rows:
            _insert_mention_edge(exchange_id, file_path)

    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('legacy_edges_backfilled', ?)",
        (datetime.now(UTC).isoformat(),),
    )


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
                id                 TEXT PRIMARY KEY,   -- sha256(source_path)
                source_path        TEXT NOT NULL UNIQUE,
                started_at         TIMESTAMP,
                last_ply_end       INT  NOT NULL DEFAULT -1,  -- 最後にインデックスした ply_end（差分用）
                parent_session_ref TEXT                       -- サブエージェントの親セッション参照（不透明値、design §4.2）
            );

            CREATE TABLE IF NOT EXISTS exchanges (
                id              TEXT PRIMARY KEY,  -- sha256(conversation_id + ":" + user_uuid)
                conversation_id TEXT NOT NULL,
                ply_start       INT  NOT NULL,
                ply_end         INT  NOT NULL,
                user_content    TEXT NOT NULL,
                agent_content   TEXT NOT NULL,
                distilled_at    TIMESTAMP,         -- NULL = 未蒸留
                distill_status  TEXT NOT NULL DEFAULT 'pending',
                git_branch      TEXT
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

            CREATE TABLE IF NOT EXISTS code_touches (
                id           TEXT PRIMARY KEY,
                exchange_id  TEXT NOT NULL,
                harness      TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                touch_kind   TEXT NOT NULL,

                locator_kind TEXT NOT NULL,
                old_start    INT,
                old_lines    INT,
                new_start    INT,
                new_lines    INT,
                old_string   TEXT,
                new_string   TEXT,

                symbol_name  TEXT,
                resolved_by  TEXT,
                added        INT NOT NULL DEFAULT 0,
                removed      INT NOT NULL DEFAULT 0,
                ts           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_code_touches_exchange ON code_touches(exchange_id);
            CREATE INDEX IF NOT EXISTS idx_code_touches_file     ON code_touches(file_path);
            CREATE INDEX IF NOT EXISTS idx_code_touches_symbol   ON code_touches(file_path, symbol_name);

            CREATE TABLE IF NOT EXISTS code_symbols (
                id          TEXT PRIMARY KEY,
                file_path   TEXT NOT NULL,
                symbol_name TEXT NOT NULL,
                symbol_kind TEXT NOT NULL,
                signature   TEXT NOT NULL,
                line        INT NOT NULL,
                end_line    INT NOT NULL,
                lang        TEXT NOT NULL,
                resolved_at TEXT NOT NULL,
                UNIQUE(file_path, symbol_name)
            );
            CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(symbol_name);
            CREATE INDEX IF NOT EXISTS idx_code_symbols_file ON code_symbols(file_path);

            CREATE TABLE IF NOT EXISTS code_edges (
                id          TEXT PRIMARY KEY,
                exchange_id TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                symbol_id   TEXT,
                edge_kind   TEXT NOT NULL,
                granularity TEXT NOT NULL,
                confidence  REAL NOT NULL,
                added       INT NOT NULL DEFAULT 0,
                ts          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_code_edges_symbol   ON code_edges(symbol_id);
            CREATE INDEX IF NOT EXISTS idx_code_edges_file     ON code_edges(file_path);
            CREATE INDEX IF NOT EXISTS idx_code_edges_exchange ON code_edges(exchange_id);

            CREATE TABLE IF NOT EXISTS file_renames (
                old_path TEXT NOT NULL,
                new_path TEXT NOT NULL,
                source   TEXT NOT NULL,
                ts       TEXT,
                PRIMARY KEY (old_path, new_path)
            );
            CREATE INDEX IF NOT EXISTS idx_file_renames_new_path ON file_renames(new_path);
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

    _backfill_legacy_code_edges(con, db_path.parent.parent)

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
