"""OpenCode session DB を exchange・code edge へ取り込む契約を検証する。

opencode.json（README 参照）は project/session/messages/parts テーブル行を JSON で
手書きしたもの。ここでは実際に SQLite ファイルへ書き戻し、index_opencode_db が
本物の DB スキーマ・SQL 経由で読めることを確認する（JSON を直接渡すテストでは
クエリや並び順のバグを検出できないため）。
"""

import json
import sqlite3
from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_opencode_db

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "opencode.json"


def _write_opencode_db(db_file: Path, project_root: Path) -> None:
    """合成ログの /repo を一時プロジェクトの絶対パスへ置き換えて SQLite に書き出す。"""
    fixture = json.loads(_FIXTURE.read_text().replace("/repo", str(project_root)))

    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL, vcs TEXT, name TEXT);
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL);
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
        );
        """
    )
    project = fixture["project"]
    con.execute(
        "INSERT INTO project (id, worktree, vcs, name) VALUES (?, ?, ?, ?)",
        (project["id"], project["worktree"], project["vcs"], project["name"]),
    )
    session = fixture["session"]
    con.execute(
        "INSERT INTO session (id, project_id, directory) VALUES (?, ?, ?)",
        (session["id"], session["project_id"], session["directory"]),
    )
    for message in fixture["messages"]:
        con.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                message["id"],
                message["session_id"],
                message["time_created"],
                message["time_created"],
                json.dumps(message["data"]),
            ),
        )
    for part in fixture["parts"]:
        con.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                part["id"],
                part["message_id"],
                part["session_id"],
                part["time_created"],
                part["time_created"],
                json.dumps(part["data"]),
            ),
        )
    con.commit()
    con.close()


def test_index_opencode_db_indexes_touches_and_edges(tmp_path: Path) -> None:
    """project.worktree が project_root に一致するセッションだけを取り込む。"""
    project_root = tmp_path / "project"
    source_dir = project_root / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")

    opencode_db = tmp_path / "opencode.db"
    _write_opencode_db(opencode_db, project_root)
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )

    assert indexed == 1
    con = get_connection(db_path)
    assert con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 1
    exchange = con.execute(
        "SELECT user_content, agent_content FROM exchanges"
    ).fetchone()
    assert exchange["user_content"] == "fs.list_dir を Result 型にして"
    assert exchange["agent_content"] == "list_dir を編集します。"
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_touches WHERE harness = 'opencode'"
        ).fetchone()[0]
        >= 2
    )
    assert con.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0] >= 2
    con.close()


def test_index_opencode_db_skips_sessions_outside_project_root(tmp_path: Path) -> None:
    """worktree が project_root と異なるセッションは取り込まない。"""
    other_root = tmp_path / "other-project"
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_db(opencode_db, other_root)

    project_root = tmp_path / "project"
    project_root.mkdir()
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_opencode_db(
        opencode_db, db_path, min_chars=1, project_root=project_root
    )

    assert indexed == 0
    con = get_connection(db_path)
    assert con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 0
    con.close()


def test_index_opencode_db_is_incremental(tmp_path: Path) -> None:
    """同じセッションを再実行しても exchange を重複登録しない。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    opencode_db = tmp_path / "opencode.db"
    _write_opencode_db(opencode_db, project_root)
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    assert (
        index_opencode_db(opencode_db, db_path, min_chars=1, project_root=project_root)
        == 1
    )
    assert (
        index_opencode_db(opencode_db, db_path, min_chars=1, project_root=project_root)
        == 0
    )
