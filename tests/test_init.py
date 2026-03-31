"""loci init コマンドのテスト — 既存 exchange の蒸留スキップ機能"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection

runner = CliRunner()


def _create_jsonl(path: Path, num_exchanges: int = 3) -> None:
    """テスト用の .jsonl ファイルを作成する（indexer の期待するフォーマットに準拠）"""
    messages = []
    for i in range(num_exchanges):
        # 50文字以上のテキストが必要（trivial フィルタ回避）
        user_text = f"User message {i}: " + "x" * 60
        agent_text = f"Agent response {i}: " + "y" * 60
        messages.append(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"user-{i}",
                    "parentUuid": f"agent-{i - 1}" if i > 0 else None,
                    "isMeta": False,
                    "timestamp": f"2026-01-01T00:0{i}:00.000Z",
                    "message": {"role": "user", "content": user_text},
                }
            )
        )
        messages.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"agent-{i}",
                    "parentUuid": f"user-{i}",
                    "timestamp": f"2026-01-01T00:0{i}:01.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": agent_text}],
                    },
                }
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(messages))


def _setup_project_with_sessions(
    tmp_path: Path, monkeypatch, num_files: int = 2, exchanges_per_file: int = 3
) -> Path:
    """プロジェクトディレクトリと Claude セッションログを作成する"""
    monkeypatch.chdir(tmp_path)

    # git root をシミュレート
    (tmp_path / ".git").mkdir()

    # Claude projects ディレクトリ
    projects_dir = tmp_path / "claude_sessions"
    projects_dir.mkdir(parents=True)

    for i in range(num_files):
        _create_jsonl(projects_dir / f"session{i}.jsonl", exchanges_per_file)

    # resolve_claude_projects_path をモックして直接 projects_dir を返す
    monkeypatch.setattr(
        "codeatrium.paths.resolve_claude_projects_path",
        lambda root: projects_dir,
    )

    return projects_dir


# ---- basic init ----


def test_init_creates_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".codeatrium" / "memory.db").exists()


def test_init_already_initialized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init"])
    assert "Already initialized" in result.output


# ---- skip-existing ----


def test_init_skip_existing_marks_all_as_skipped(tmp_path, monkeypatch):
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=2, exchanges_per_file=3)

    result = runner.invoke(app, ["init", "--skip-existing"])
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    null_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NULL"
    ).fetchone()[0]
    skipped_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at = 'skipped'"
    ).fetchone()[0]
    con.close()

    assert null_count == 0
    assert skipped_count > 0
    assert "skipped" in result.output.lower() or "Marked" in result.output


# ---- distill-limit ----


def test_init_distill_limit_keeps_recent(tmp_path, monkeypatch):
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=2, exchanges_per_file=3)

    result = runner.invoke(app, ["init", "--distill-limit", "2"])
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    null_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NULL"
    ).fetchone()[0]
    skipped_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at = 'skipped'"
    ).fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    con.close()

    # 直近2件だけ蒸留対象（NULL）、残りは skipped
    assert null_count == 2
    assert skipped_count == total - 2


# ---- small count skips prompt ----


def test_init_prompt_distill_all(tmp_path, monkeypatch):
    """対話プロンプトで [3] を選ぶと全件蒸留対象"""
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=1, exchanges_per_file=3)

    result = runner.invoke(app, ["init"], input="3\n")
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    null_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NULL"
    ).fetchone()[0]
    skipped_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at = 'skipped'"
    ).fetchone()[0]
    con.close()

    assert null_count > 0
    assert skipped_count == 0


def test_init_prompt_skip_all(tmp_path, monkeypatch):
    """対話プロンプトで [1] を選ぶと全件スキップ"""
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=1, exchanges_per_file=3)

    result = runner.invoke(app, ["init"], input="1\n")
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    null_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NULL"
    ).fetchone()[0]
    skipped_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at = 'skipped'"
    ).fetchone()[0]
    con.close()

    assert null_count == 0
    assert skipped_count > 0
