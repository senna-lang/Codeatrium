"""OpenCode 取り込みのロバスト性（issue #21）を検証する。

_load_opencode_raw_entries / index_opencode_db の4つの既知バグを対象にする:
  1. 1行の破損（不正 JSON・NULL）が DB 全体の取り込みを中断してはならない
  2. project.worktree が NULL のとき os.path.realpath で例外を起こしてはならない
  3. ply_start（位置添字）ベースのカーソルは、time_created が既存行より古い新規
     メッセージの到着で添字が全体シフトし、既取り込みターンを再emit（重複登録）する
  4. sqlite3 の file: URI は DB パスに ?/#/空白 を含むと誤解釈されるため、
     percent-encode してから接続しなければならない
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codeatrium.core.ingest import ingest_parse_result
from codeatrium.core.models import CanonicalExchange, CanonicalSession, ParseResult
from codeatrium.db import get_connection, init_db
from codeatrium.indexer import (
    _load_opencode_raw_entries,
    index_opencode_db,
    parse_opencode_exchanges,
)

_SCHEMA = """
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, vcs TEXT, name TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
"""


def _user_message(msg_id: str, session_id: str, time_created: int) -> tuple:
    return (
        msg_id,
        session_id,
        time_created,
        time_created,
        json.dumps({"role": "user"}),
    )


def _text_part(part_id: str, message_id: str, session_id: str, time_created: int, text: str) -> tuple:
    return (
        part_id,
        message_id,
        session_id,
        time_created,
        time_created,
        json.dumps({"type": "text", "text": text}),
    )


def _connect(db_file: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_file)
    con.executescript(_SCHEMA)
    return con


def test_corrupt_row_does_not_abort_whole_db_ingestion(tmp_path: Path) -> None:
    """破損した1行（不正 JSON）があっても、他セッションの取り込みは継続する。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    opencode_db = tmp_path / "opencode.db"

    con = _connect(opencode_db)
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj1", str(project_root), "git", "repo"),
    )
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        ("ses_ok", "proj1", str(project_root)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg_ok", "ses_ok", 1000),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt_ok", "msg_ok", "ses_ok", 1001, "a" * 60),
    )
    # 破損行: data が不正 JSON。同一セッション内に混在させる。
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        ("msg_corrupt", "ses_ok", 1002, 1002, "{not-valid-json"),
    )
    # 破損行: time_created が NULL。
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        ("msg_null_time", "ses_ok", None, None, json.dumps({"role": "user"})),
    )
    con.commit()
    con.close()

    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    # 破損行があっても例外を送出せず、正常な exchange は取り込まれる。
    indexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    assert indexed == 1


def test_non_dict_json_row_is_skipped_not_crashed(tmp_path: Path) -> None:
    """data が構文的に正しい JSON でも dict でない（null/配列/文字列）行はスキップする。

    json.loads はこれらの値に対して例外を出さずに成功するため、JSONDecodeError だけを
    捕捉するガードはこの形の破損行を素通りさせてしまい、parse_opencode_exchanges の
    entry["data"].get(...) 呼び出しで AttributeError を起こして DB 全体の取り込みを
    中断させる（#21 が排除しようとした失敗モードそのものが別の入力形で再発する）。
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    opencode_db = tmp_path / "opencode.db"

    con = _connect(opencode_db)
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj1", str(project_root), "git", "repo"),
    )
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        ("ses_ok", "proj1", str(project_root)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg_ok", "ses_ok", 1000),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt_ok", "msg_ok", "ses_ok", 1001, "z" * 60),
    )
    # 破損行: data が構文的に正しい JSON だが dict ではない（null / 配列 / 文字列）。
    for bad_id, bad_json in (
        ("msg_null", "null"),
        ("msg_list", "[]"),
        ("msg_str", '"just text"'),
    ):
        con.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (bad_id, "ses_ok", 1003, 1003, bad_json),
        )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("prt_null", "msg_ok", "ses_ok", 1004, 1004, "null"),
    )
    con.commit()
    con.close()

    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    assert indexed == 1


def test_worktree_none_is_skipped_not_raised(tmp_path: Path) -> None:
    """project.worktree が NULL の行は os.path.realpath に渡さずスキップする。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    opencode_db = tmp_path / "opencode.db"

    con = _connect(opencode_db)
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj_null", None, "git", "orphan"),
    )
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj_ok", str(project_root), "git", "repo"),
    )
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        ("ses_ok", "proj_ok", str(project_root)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg_ok", "ses_ok", 1000),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt_ok", "msg_ok", "ses_ok", 1001, "b" * 60),
    )
    con.commit()
    con.close()

    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    assert indexed == 1


def test_out_of_order_message_does_not_reemit_existing_exchange(tmp_path: Path) -> None:
    """time_created が既存より古い新規メッセージの到着で ply 添字が全体シフトしても、
    既に取り込み済みのターンを重複登録しない（安定 message-id ベースのカーソル）。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    opencode_db = tmp_path / "opencode.db"

    con = _connect(opencode_db)
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj1", str(project_root), "git", "repo"),
    )
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        ("ses1", "proj1", str(project_root)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg1", "ses1", 1000),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt1", "msg1", "ses1", 1001, "c" * 60),
    )
    con.commit()
    con.close()

    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    first = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    assert first == 1

    # msg0 が msg1 より「古い」time_created で後から到着する
    # （バックフィル・クロックスキュー等の実運用シナリオ）。
    # (time_created, id) 順ソートで msg0/prt0 が msg1/prt1 の手前に入り、
    # msg1 の raw_entries 内の位置添字（ply_start）が変わる。
    con = sqlite3.connect(opencode_db)
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg0", "ses1", 500),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt0", "msg0", "ses1", 501, "d" * 60),
    )
    con.commit()
    con.close()

    second = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    # msg0 の1件だけが新規に登録され、msg1 は重複登録されない。
    assert second == 1

    con = get_connection(db_path)
    contents = sorted(
        row[0] for row in con.execute("SELECT user_content FROM exchanges")
    )
    con.close()
    # 位置添字ベースのカーソルだと、msg1 は ply_start シフトで再emit（重複）される
    # 一方、真に新規な msg0 は旧 msg1 と偶然ハッシュ衝突して黙殺され得る
    # （id ベースのカーソルなら両方が正確に1件ずつ残る）。
    assert contents == sorted(["c" * 60, "d" * 60])


def test_db_path_with_special_uri_characters_is_opened_correctly(tmp_path: Path) -> None:
    """DB パスに '#' を含む場合でも file: URI が誤解釈されず正しいファイルを開く。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    # '#' は file: URI のフラグメント区切りとして誤解釈され得る文字。
    opencode_db = tmp_path / "op#session.db"

    con = _connect(opencode_db)
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj1", str(project_root), "git", "repo"),
    )
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        ("ses1", "proj1", str(project_root)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg1", "ses1", 1000),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt1", "msg1", "ses1", 1001, "e" * 60),
    )
    con.commit()
    con.close()

    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    assert indexed == 1



def test_upgrade_from_legacy_position_based_cursor_does_not_duplicate(
    tmp_path: Path,
) -> None:
    """パッチ適用前に position ベースの source_turn_id (str(ply_start)) で取り込み
    済みの exchange は、id ベースのカーソルへ移行した後の再取り込みで重複登録
    されない（旧スキームの数値 id を known set のフォールバックとして扱う）。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    opencode_db = tmp_path / "opencode.db"

    con = _connect(opencode_db)
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        ("proj1", str(project_root), "git", "repo"),
    )
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        ("ses1", "proj1", str(project_root)),
    )
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        _user_message("msg1", "ses1", 1000),
    )
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _text_part("prt1", "msg1", "ses1", 1001, "legacy " + "x" * 60),
    )
    con.commit()
    con.close()

    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    # 旧スキーム（source_turn_id=str(ply_start)）で取り込み済みの状態を直接構築する
    # （このパッチ以前の index_opencode_db が生成していた永続化結果を模する）。
    src = sqlite3.connect(f"file:{opencode_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    raw_entries, started_at = _load_opencode_raw_entries(src, "ses1")
    src.close()
    source_path = f"{opencode_db}#ses1"
    exchanges = parse_opencode_exchanges(source_path, raw_entries, min_chars=1)
    assert len(exchanges) == 1
    legacy_exchange = exchanges[0]
    legacy_turn_id = str(legacy_exchange.ply_start)

    con = get_connection(db_path)
    ingest_parse_result(
        con,
        CanonicalSession(
            harness="opencode",
            source_session_id="ses1",
            primary_ref=source_path,
            project_key=str(project_root),
            started_at=started_at,
        ),
        ParseResult(
            exchanges=(
                CanonicalExchange(
                    harness="opencode",
                    session_ref=(
                        f"{source_path}#ply="
                        f"{legacy_exchange.ply_start}-{legacy_exchange.ply_end}"
                    ),
                    source_session_id="ses1",
                    source_turn_id=legacy_turn_id,
                    ply_start=legacy_exchange.ply_start,
                    ply_end=legacy_exchange.ply_end,
                    user_content=legacy_exchange.user_content,
                    agent_content=legacy_exchange.agent_content,
                    files_touched=tuple(legacy_exchange.files),
                    git_branch=legacy_exchange.git_branch,
                ),
            ),
            next_cursor=f"v1:ply:{legacy_exchange.ply_end}",
        ),
    )
    con.commit()
    con.close()

    con = get_connection(db_path)
    pre_upgrade_count = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    con.close()
    assert pre_upgrade_count == 1

    # パッチ適用後の index_opencode_db を同じ opencode DB に対して再実行する。
    reindexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )
    assert reindexed == 0

    con = get_connection(db_path)
    post_upgrade_count = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    con.close()
    assert post_upgrade_count == 1