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

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_opencode_db

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
