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

def _migrate_v11_add_canonical_sessions(con: sqlite3.Connection) -> None:
    """Migration v11: persist harness-neutral sessions and exchange provenance."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id                TEXT PRIMARY KEY,
            harness           TEXT NOT NULL,
            source_session_id TEXT NOT NULL,
            primary_ref       TEXT NOT NULL,
            project_key       TEXT NOT NULL,
            cursor            TEXT,
            cursor_version    INTEGER NOT NULL DEFAULT 1,
            started_at        TEXT,
            title             TEXT,
            git_branch_last   TEXT,
            updated_at        TEXT NOT NULL,
            UNIQUE(harness, source_session_id)
        )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_harness ON sessions(harness)"
    )
    exchanges_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exchanges'"
    ).fetchone()
    if exchanges_exists is None:
        return

    exchange_columns = {
        row[1] for row in con.execute("PRAGMA table_info(exchanges)").fetchall()
    }
    for name, definition in (
        ("session_id", "TEXT"),
        ("harness", "TEXT"),
        ("session_ref", "TEXT"),
        ("source_session_id", "TEXT"),
        ("source_turn_id", "TEXT"),
        ("agent_model", "TEXT"),
        ("agent_provider", "TEXT"),
    ):
        if name not in exchange_columns:
            con.execute(f"ALTER TABLE exchanges ADD COLUMN {name} {definition}")

    rows = con.execute(
        "SELECT id, source_path, started_at, last_ply_end FROM conversations"
    ).fetchall()
    for row in rows:
        source_path = row["source_path"]
        session_id = hashlib.sha256(f"claude:{source_path}".encode()).hexdigest()
        cursor = f"v1:ply:{row['last_ply_end']}"
        con.execute(
            """
            INSERT OR IGNORE INTO sessions
                (id, harness, source_session_id, primary_ref, project_key, cursor,
                 started_at, updated_at)
            VALUES (?, 'claude', ?, ?, '', ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (session_id, source_path, source_path, cursor, row["started_at"], row["started_at"]),
        )
        con.execute(
            """
            UPDATE exchanges
            SET session_id = ?,
                harness = COALESCE(harness, 'claude'),
                session_ref = COALESCE(
                    session_ref, ? || '#ply=' || ply_start || '-' || ply_end
                ),
                source_session_id = COALESCE(source_session_id, ?),
                source_turn_id = COALESCE(source_turn_id, CAST(ply_start AS TEXT))
            WHERE conversation_id = ?
            """,
            (session_id, source_path, source_path, row["id"]),
        )

def _backfill_exchange_provenance(con: sqlite3.Connection) -> None:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exchanges'"
    ).fetchone()
    if exists is None:
        return
    rows = con.execute(
        """
        SELECT e.id, e.conversation_id, e.ply_start, e.ply_end, c.source_path,
               c.started_at
        FROM exchanges e JOIN conversations c ON c.id = e.conversation_id
        WHERE e.session_id IS NULL OR e.harness IS NULL
        """
    ).fetchall()
    for row in rows:
        source_path = row["source_path"]
        if "opencode.db#" in source_path:
            harness = "opencode"
            source_session_id = source_path.rsplit("#", 1)[1]
        elif "rollout-" in source_path:
            harness = "codex"
            source_session_id = source_path
        elif "/.omp/" in source_path:
            harness = "omp-pi"
            source_session_id = source_path
        elif "/.grok/" in source_path:
            harness = "grok"
            source_session_id = source_path
        else:
            harness = "claude"
            source_session_id = source_path
        session_id = hashlib.sha256(
            f"{harness}:{source_session_id}".encode()
        ).hexdigest()
        con.execute(
            """
            INSERT OR IGNORE INTO sessions (
                id, harness, source_session_id, primary_ref, project_key,
                cursor, cursor_version, started_at, updated_at
            ) VALUES (?, ?, ?, ?, '', NULL, 1, ?, CURRENT_TIMESTAMP)
            """,
            (session_id, harness, source_session_id, source_path, row["started_at"]),
        )
        con.execute(
            """
            UPDATE exchanges SET
                session_id = COALESCE(session_id, ?),
                harness = COALESCE(harness, ?),
                session_ref = COALESCE(
                    session_ref, ? || '#ply=' || ply_start || '-' || ply_end
                ),
                source_session_id = COALESCE(source_session_id, ?),
                source_turn_id = COALESCE(source_turn_id, CAST(ply_start AS TEXT))
            WHERE id = ?
            """,
            (session_id, harness, source_path, source_session_id, row["id"]),
        )




def _migrate_v12_add_exchange_conversation_ply_index(con: sqlite3.Connection) -> None:
    """Migration v12: exchanges(conversation_id, ply_start) に複合インデックスを追加する。

    周辺コンテキスト取得（design: ply隣接レーン）が会話単位で ply 順に exchange を
    引くため。従来の idx_exchanges_canonical_id とは別物で、これが無いと
    conversation_id 絞り込みがフルスキャンになる。
    """
    exchanges_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='exchanges'"
    ).fetchone()
    if exchanges_exists is None:
        return
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_exchanges_conversation_ply "
        "ON exchanges(conversation_id, ply_start)"
    )


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
    _migrate_v11_add_canonical_sessions,
    _migrate_v12_add_exchange_conversation_ply_index,
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


def _backfill_touch_time_symbol_edges(con: sqlite3.Connection, project_root: Path) -> None:
    """Upgrade stale file-granularity `code_edges` to symbol-level where a
    point-in-time git blob now allows a match.

    Historical `code_touches` rows carry line ranges frozen at the moment of
    that edit. Before this backfill, symbol resolution always read the
    *live* working-tree file at index time, so a touch's line numbers drift
    out of alignment with the file's *current* symbol boundaries the more
    the file is edited afterward — old touches degrade to file-level
    `mention` edges even though a real symbol-level match exists against
    the file as it looked when that touch happened. `core.ingest` now
    resolves new touches against the git blob nearest their own timestamp;
    this backfill applies the same resolution retroactively to touches
    already persisted under the old (live-disk) behavior.

    Purely additive: only inserts new `line`-granularity edges (via the
    same deterministic id `touches_to_edges` always uses, so re-running is
    a no-op). Never deletes the coarser `file`-granularity edges the old
    behavior already created — those stay harmless alongside a precise
    match (`resolve_u1`'s symbol tier only ever joins on a non-null
    `symbol_id`, so a leftover file-level row is never consulted once a
    line-level row for the same exchange/file exists).

    project_root-dependent (like `_backfill_legacy_code_edges`), so this
    stays outside `_MIGRATIONS`/`user_version` and runs once via a `meta`
    completion flag.
    """
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    done = con.execute(
        "SELECT 1 FROM meta WHERE key = 'touch_time_symbol_edges_backfilled'"
    ).fetchone()
    if done is not None:
        return

    code_touches_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='code_touches'"
    ).fetchone()
    if code_touches_exists is None or not project_root.is_dir():
        return

    from codeatrium.code_touches import touches_to_edges
    from codeatrium.core.ingest import _resolve_symbols_at
    from codeatrium.models import CodeTouch, LineRange
    from codeatrium.resolver import Symbol, SymbolResolver
    from codeatrium.utils import sha256

    resolver = SymbolResolver()
    symbol_cache: dict[tuple[str, str | None], list[Symbol]] = {}
    resolved_at = datetime.now(UTC).isoformat()

    rows = con.execute(
        """
        SELECT exchange_id, harness, tool_call_id, file_path, touch_kind,
               old_start, old_lines, new_start, new_lines, added, removed, ts
        FROM code_touches
        WHERE locator_kind = 'line'
        """
    ).fetchall()

    for row in rows:
        rel_path = row["file_path"]
        cache_key = (rel_path, row["ts"])
        symbols = symbol_cache.get(cache_key)
        if symbols is None:
            symbols = _resolve_symbols_at(
                resolver,
                project_root,
                str(project_root / rel_path),
                rel_path,
                row["ts"],
            )
            symbol_cache[cache_key] = symbols
        if not symbols:
            continue

        touch = CodeTouch(
            harness=row["harness"],
            tool_call_id=row["tool_call_id"],
            file_path=str(project_root / rel_path),
            touch_kind=row["touch_kind"],
            locators=(
                LineRange(
                    old_start=row["old_start"],
                    old_lines=row["old_lines"],
                    new_start=row["new_start"],
                    new_lines=row["new_lines"],
                ),
            ),
            added=row["added"],
            removed=row["removed"],
            ts=row["ts"],
        )
        edges = touches_to_edges(
            touch, exchange_id=row["exchange_id"], rel_file_path=rel_path, symbols=symbols
        )
        line_edges = [e for e in edges if e.granularity == "line"]
        if not line_edges:
            continue

        for symbol in symbols:
            symbol_id = sha256(f"{rel_path}:{symbol.symbol_name}")
            con.execute(
                """
                INSERT OR IGNORE INTO code_symbols
                    (id, file_path, symbol_name, symbol_kind, signature,
                     line, end_line, lang, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol_id,
                    rel_path,
                    symbol.symbol_name,
                    symbol.symbol_kind,
                    symbol.signature,
                    symbol.line,
                    symbol.end_line,
                    symbol.lang,
                    resolved_at,
                ),
            )
        for edge in line_edges:
            con.execute(
                """
                INSERT INTO code_edges
                    (id, exchange_id, file_path, symbol_id, edge_kind,
                     granularity, confidence, added, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    edge.id,
                    edge.exchange_id,
                    edge.file_path,
                    edge.symbol_id,
                    edge.edge_kind,
                    edge.granularity,
                    edge.confidence,
                    edge.added,
                    edge.ts,
                ),
            )

    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('touch_time_symbol_edges_backfilled', ?)",
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



def _backfill_canonical_exchange_ids(con: sqlite3.Connection) -> None:
    """Record canonical identities without rewriting stable exchange IDs."""
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exchanges'"
    ).fetchone() is None:
        return
    columns = {row[1] for row in con.execute("PRAGMA table_info(exchanges)")}
    if "canonical_exchange_id" not in columns:
        con.execute("ALTER TABLE exchanges ADD COLUMN canonical_exchange_id TEXT")
        con.execute(
            """
            CREATE UNIQUE INDEX idx_exchanges_canonical_id
            ON exchanges(canonical_exchange_id)
            WHERE canonical_exchange_id IS NOT NULL
            """
        )
    rows = con.execute(
        """
        SELECT id, harness, source_session_id, source_turn_id FROM exchanges
        WHERE canonical_exchange_id IS NULL AND harness IS NOT NULL
          AND source_session_id IS NOT NULL AND source_turn_id IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        canonical_id = hashlib.sha256(
            f"{row['harness']}:{row['source_session_id']}:{row['source_turn_id']}".encode()
        ).hexdigest()
        con.execute(
            "UPDATE exchanges SET canonical_exchange_id = ? WHERE id = ?",
            (canonical_id, row["id"]),
        )


def _backfill_parent_session_ref(con: sqlite3.Connection) -> None:
    """既存 conversations の parent_session_ref を埋める（design §2.3・§4.2）。

    サブエージェントの transcript パス規約は各ハーネスアダプターだけが知っている
    ——core はパス形状を解釈しない、という設計方針を backfill でも守り、判定は
    ingest と同じ port（`JsonlLogSource.parent_ref_resolver`、
    `adapters/harness/registry.py:detected_jsonl_sources()`）に委譲する
    （issue #39: persistence 層が特定ハーネスを直 import する抜け道を解消）。
    冪等（NULL の行だけ処理）。
    """
    conversations_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
    ).fetchone()
    if conversations_exists is None:
        return
    columns = {row[1] for row in con.execute("PRAGMA table_info(conversations)")}
    if "parent_session_ref" not in columns:
        return

    from codeatrium.adapters.harness.registry import detected_jsonl_sources

    resolvers = [
        source.parent_ref_resolver
        for source in detected_jsonl_sources()
        if source.parent_ref_resolver is not None
    ]
    if not resolvers:
        return

    rows = con.execute(
        "SELECT id, source_path FROM conversations WHERE parent_session_ref IS NULL"
    ).fetchall()
    for row in rows:
        path = Path(row["source_path"])
        for resolver in resolvers:
            ref = resolver(path)
            if ref is not None:
                con.execute(
                    "UPDATE conversations SET parent_session_ref = ? WHERE id = ?",
                    (ref, row["id"]),
                )
                break


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
            CREATE TABLE IF NOT EXISTS sessions (
                id                TEXT PRIMARY KEY,
                harness           TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                primary_ref       TEXT NOT NULL,
                project_key       TEXT NOT NULL,
                cursor            TEXT,
                cursor_version    INTEGER NOT NULL DEFAULT 1,
                started_at        TEXT,
                title             TEXT,
                git_branch_last   TEXT,
                updated_at        TEXT NOT NULL,
                UNIQUE(harness, source_session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_harness ON sessions(harness);

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
                git_branch        TEXT,
                session_id        TEXT,
                harness           TEXT,
                session_ref       TEXT,
                source_session_id TEXT,
                source_turn_id    TEXT,
                agent_model       TEXT,
                agent_provider    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exchanges_conversation_ply ON exchanges(conversation_id, ply_start);

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
    _backfill_exchange_provenance(con)
    _backfill_canonical_exchange_ids(con)
    _backfill_parent_session_ref(con)

    _backfill_legacy_code_edges(con, db_path.parent.parent)
    _backfill_touch_time_symbol_edges(con, db_path.parent.parent)

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
