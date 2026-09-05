"""loci index コマンドのテスト — 未初期化リポジトリでの DB 自動生成防止"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db

runner = CliRunner()


def _write_opencode_db(db_file: Path, project_root: Path) -> None:
    """合成ログの /repo を一時プロジェクトの絶対パスへ置き換えて SQLite に書き出す。

    既定の index_min_chars（50文字）を超えるよう会話文を水増しする。
    """
    fixture_path = Path(__file__).parent / "fixtures" / "harness_logs" / "opencode.json"
    fixture = json.loads(
        fixture_path.read_text()
        .replace("/repo", str(project_root))
        .replace("fs.list_dir を Result 型にして", "fs.list_dir を Result 型にして。" * 4)
        .replace("list_dir を編集します。", "list_dir を編集します。" * 4)
    )

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


def test_index_rejects_uninitialized_repo(tmp_path: Path, monkeypatch) -> None:
    """loci init していないリポジトリで loci index を実行するとエラーになる"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["index"])
    assert result.exit_code != 0
    assert "loci init" in result.output
    # .codeatrium ディレクトリが作成されていないこと
    assert not (tmp_path / ".codeatrium").exists()


def test_index_works_after_init(tmp_path: Path, monkeypatch) -> None:
    """loci init 済みのリポジトリでは loci index が正常に動作する"""
    monkeypatch.chdir(tmp_path)
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    db = codeatrium_dir / "memory.db"
    init_db(db)

    result = runner.invoke(app, ["index"])
    # Claude projects dir が見つからないので exit(1) だが、init ガードは通過
    assert "loci init" not in result.output


def test_index_ingests_codex_rollout(tmp_path: Path, monkeypatch) -> None:
    """--harness codex は rollout JSONL をコード編集記録まで取り込む。"""
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / ".codeatrium" / "memory.db")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")
    (source_dir / "new_name.py").write_text("x = 2\n")

    fixture = Path(__file__).parent / "fixtures" / "harness_logs" / "codex.jsonl"
    rollout_dir = tmp_path / "sessions"
    rollout_dir.mkdir()
    rollout = rollout_dir / "rollout-synthetic.jsonl"
    rollout.write_text(
        fixture.read_text()
        .replace('"/repo', f'"{tmp_path}')
        .replace(
            "fs.list_dir を Result 型にして", "fs.list_dir を Result 型にして。" * 4
        )
        .replace("list_dir を編集します。", "list_dir を編集します。" * 4)
    )

    result = runner.invoke(
        app,
        ["index", "--harness", "codex", "--path", str(rollout_dir)],
    )

    assert result.exit_code == 0
    assert "Indexed 1 file(s), 1 exchange(s)." in result.output
    con = get_connection(tmp_path / ".codeatrium" / "memory.db")
    assert con.execute("SELECT COUNT(*) FROM code_touches").fetchone()[0] >= 4
    assert con.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0] >= 4
    assert con.execute("SELECT COUNT(*) FROM file_renames").fetchone()[0] == 1
    con.close()

def test_index_codex_excludes_foreign_project_rollout(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / ".codeatrium" / "memory.db")
    (tmp_path / ".codeatrium" / "config.toml").write_text(
        "[index]\nmin_chars = 1\n"
    )
    fixtures = Path(__file__).parent / "fixtures" / "harness_logs" / "codex.jsonl"
    rollout_dir = tmp_path / "sessions"
    rollout_dir.mkdir()
    (rollout_dir / "rollout-local.jsonl").write_text(
        fixtures.read_text().replace("/repo", str(tmp_path))
    )
    (rollout_dir / "rollout-foreign.jsonl").write_text(
        fixtures.read_text().replace("/repo", str(tmp_path.parent / "foreign"))
    )
    con = get_connection(tmp_path / ".codeatrium" / "memory.db")
    con.execute(
        "INSERT INTO conversations (id, source_path) VALUES (?, ?)",
        ("foreign-conversation", str(rollout_dir / "rollout-foreign.jsonl")),
    )
    con.execute(
        """
        INSERT INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content,
             harness, session_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "foreign-codex",
            "foreign-conversation",
            0,
            1,
            "foreign user",
            "foreign agent",
            "codex",
            f"{rollout_dir / 'rollout-foreign.jsonl'}#ply=0-1",
        ),
    )
    con.commit()
    con.close()

    result = runner.invoke(
        app, ["index", "--harness", "codex", "--path", str(rollout_dir)]
    )

    assert result.exit_code == 0
    assert "Indexed 1 file(s)" in result.output
    con = get_connection(tmp_path / ".codeatrium" / "memory.db")
    assert con.execute(
        "SELECT 1 FROM exchanges WHERE id = 'foreign-codex'"
    ).fetchone() is None
    con.close()


def test_index_ingests_opencode_session_db(tmp_path: Path, monkeypatch) -> None:
    """--harness opencode は project_root にひも付くセッションだけを取り込む。"""
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / ".codeatrium" / "memory.db")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")

    opencode_db = tmp_path / "opencode.db"
    _write_opencode_db(opencode_db, tmp_path)

    result = runner.invoke(
        app,
        ["index", "--harness", "opencode", "--path", str(opencode_db)],
    )

    assert result.exit_code == 0
    assert "Indexed 1 file(s), 1 exchange(s)." in result.output
    con = get_connection(tmp_path / ".codeatrium" / "memory.db")
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_touches WHERE harness = 'opencode'"
        ).fetchone()[0]
        >= 2
    )
    assert con.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0] >= 2
    con.close()


def test_index_ingests_omp_pi_session(tmp_path: Path, monkeypatch) -> None:
    """--harness omp-pi は相対パスのパッチを cwd で絶対化して取り込む。"""
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / ".codeatrium" / "memory.db")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")

    fixture = Path(__file__).parent / "fixtures" / "harness_logs" / "omp_pi.jsonl"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session = sessions_dir / "2026-08-01T00-00-00-000Z_synthetic.jsonl"
    session.write_text(
        fixture.read_text()
        .replace('"/repo', f'"{tmp_path}')
        .replace("list_dir を Result 型にして", "list_dir を Result 型にして。" * 4)
    )

    result = runner.invoke(
        app,
        ["index", "--harness", "omp-pi", "--path", str(sessions_dir)],
    )

    assert result.exit_code == 0
    con = get_connection(tmp_path / ".codeatrium" / "memory.db")
    touched = {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT file_path FROM code_touches WHERE harness = 'omp-pi'"
        )
    }
    con.close()
    assert {"src/fs.py", "src/result.py"} <= touched
