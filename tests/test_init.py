"""loci init コマンドのテスト — 既存 exchange の蒸留スキップ機能"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection

runner = CliRunner()


def _create_jsonl(
    path: Path, num_exchanges: int = 3, char_sizes: list[int] | None = None
) -> None:
    """テスト用の .jsonl ファイルを作成する（indexer の期待するフォーマットに準拠）

    char_sizes: 各 exchange の合計文字数の目安リスト。指定時は num_exchanges より優先。
    """
    if char_sizes is not None:
        num_exchanges = len(char_sizes)
    messages = []
    for i in range(num_exchanges):
        if char_sizes is not None:
            # 指定文字数を user/agent で折半
            half = max(1, char_sizes[i] // 2)
            user_text = "u" * half
            agent_text = "a" * half
        else:
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
    tmp_path: Path,
    monkeypatch,
    num_files: int = 2,
    exchanges_per_file: int = 3,
    char_sizes: list[int] | None = None,
) -> Path:
    """プロジェクトディレクトリと Claude セッションログを作成する"""
    monkeypatch.chdir(tmp_path)

    # git root をシミュレート
    (tmp_path / ".git").mkdir()

    # Claude projects ディレクトリ
    projects_dir = tmp_path / "claude_sessions"
    projects_dir.mkdir(parents=True)

    for i in range(num_files):
        _create_jsonl(
            projects_dir / f"session{i}.jsonl",
            exchanges_per_file,
            char_sizes=char_sizes,
        )

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


def test_init_creates_claude_md_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.exists()
    content = claude_md.read_text()
    assert "<!-- BEGIN CODEATRIUM -->" in content
    assert "<!-- END CODEATRIUM -->" in content
    assert "loci prime" in content


def test_init_appends_to_existing_claude_md(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# My Project\n\nExisting content.\n")
    runner.invoke(app, ["init"])
    content = claude_md.read_text()
    assert content.startswith("# My Project")
    assert "<!-- BEGIN CODEATRIUM -->" in content


def test_init_updates_existing_codeatrium_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "# Proj\n\n"
        "<!-- BEGIN CODEATRIUM -->\nold content\n<!-- END CODEATRIUM -->\n\n"
        "## Other\n"
    )
    runner.invoke(app, ["init"])
    content = claude_md.read_text()
    assert "old content" not in content
    assert "loci prime" in content
    assert "## Other" in content


def test_init_already_initialized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init"])
    assert "Already initialized" in result.output


# ---- skip-existing ----


def test_init_skip_existing_marks_all_as_skipped(tmp_path, monkeypatch):
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=2, exchanges_per_file=3)

    # min_chars プロンプト [1]=50 (default)
    result = runner.invoke(app, ["init", "--skip-existing"], input="1\n")
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

    # min_chars [1]=50 → priority [1]=recent → distill now [1]=no
    result = runner.invoke(app, ["init", "--distill-limit", "2"], input="1\n1\n1\n")
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

    # min_chars [1]=50 → distill [3]=全件 → distill now [1]=no
    result = runner.invoke(app, ["init"], input="1\n3\n1\n")
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

    # min_chars プロンプト [1]=50 (default) → 蒸留プロンプト [1]=全件スキップ
    result = runner.invoke(app, ["init"], input="1\n1\n")
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


# ---- min-chars ----


def test_init_min_chars_flag(tmp_path, monkeypatch):
    """--min-chars 200 で短い exchange が除外される"""
    # 60文字 x2, 250文字 x1 の exchange を持つセッション
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=1, char_sizes=[60, 60, 250]
    )

    result = runner.invoke(app, ["init", "--min-chars", "200", "--skip-existing"])
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    total = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    con.close()

    # 250文字の1件だけインデックスされる
    assert total == 1


def test_init_min_chars_prompt_select_100(tmp_path, monkeypatch):
    """対話プロンプトで [2] (100 chars) を選んで正しくフィルタ"""
    # 60文字 x2, 150文字 x1, 250文字 x1
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=1, char_sizes=[60, 60, 150, 250]
    )

    # min_chars プロンプト [2]=100 → 蒸留プロンプト [1]=全件スキップ
    result = runner.invoke(app, ["init"], input="2\n1\n")
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    total = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    con.close()

    # 150文字 + 250文字 の2件がインデックスされる
    assert total == 2


def test_init_min_chars_flag_skips_prompt(tmp_path, monkeypatch):
    """--min-chars を指定すると min_chars の対話プロンプトが出ない"""
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=1, exchanges_per_file=3
    )

    # --skip-existing で蒸留プロンプトもスキップ → 対話入力なしで完了
    result = runner.invoke(app, ["init", "--min-chars", "50", "--skip-existing"])
    assert result.exit_code == 0
    assert "Min chars threshold" not in result.output


# ---- distill priority ----


def test_init_distill_priority_longest(tmp_path, monkeypatch):
    """priority で [2] (longest) を選ぶと短い exchange がスキップされる"""
    # 60文字 x1, 150文字 x1, 300文字 x1
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=1, char_sizes=[60, 150, 300]
    )

    # min_chars [1]=50 → distill [4]=custom → 1件 → priority [2]=longest → distill now [1]=no
    result = runner.invoke(app, ["init"], input="1\n4\n1\n2\n1\n")
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    # 蒸留対象(NULL) は最長の1件だけ
    distill_targets = con.execute(
        "SELECT LENGTH(user_content) + LENGTH(agent_content) AS len "
        "FROM exchanges WHERE distilled_at IS NULL"
    ).fetchall()
    con.close()

    assert len(distill_targets) == 1
    # 最長の exchange (300文字) が残っている
    assert distill_targets[0]["len"] >= 300


def test_init_distill_priority_recent(tmp_path, monkeypatch):
    """priority で [1] (recent) を選ぶと古い exchange がスキップされる"""
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=1, exchanges_per_file=3
    )

    # min_chars [1]=50 → distill [4]=custom → 1件 → priority [1]=recent → distill now [1]=no
    result = runner.invoke(app, ["init"], input="1\n4\n1\n1\n1\n")
    assert result.exit_code == 0

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    # 蒸留対象(NULL) は最新の1件だけ
    target = con.execute(
        "SELECT ply_start FROM exchanges WHERE distilled_at IS NULL"
    ).fetchall()
    skipped = con.execute(
        "SELECT ply_start FROM exchanges WHERE distilled_at = 'skipped'"
    ).fetchall()
    con.close()

    assert len(target) == 1
    # 最新の ply_start が残っている（skipped のどれよりも大きい）
    assert all(target[0]["ply_start"] > s["ply_start"] for s in skipped)
