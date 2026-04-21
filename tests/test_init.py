"""loci init コマンドのテスト — 既存 exchange の蒸留スキップ機能"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """$HOME を tmp に隔離して install_hooks が実環境の ~/.claude を触らないようにする"""
    monkeypatch.setenv("HOME", str(tmp_path))


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
        lambda _root: projects_dir,
    )

    return projects_dir


# ---- basic init ----


def test_init_creates_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".codeatrium" / "memory.db").exists()


def test_init_prints_banner(tmp_path, monkeypatch):
    """init 実行時に ASCII アートバナーとサブタイトルが表示される"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    # pagga figlet バナー特有の文字列
    assert "░█▀▀░█▀█░█▀▄" in result.output
    assert "memory palace for AI coding agents" in result.output


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


def test_init_non_git_does_not_traverse_parent(tmp_path, monkeypatch):
    """git 外ディレクトリで init すると親の .codeatrium を拾わない"""
    # 親に .codeatrium を配置
    parent_db = tmp_path / ".codeatrium" / "memory.db"
    parent_db.parent.mkdir()
    parent_db.touch()

    # 子ディレクトリ（git なし）で init
    child = tmp_path / "child_project"
    child.mkdir()
    monkeypatch.chdir(child)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "Initialized" in result.output
    assert (child / ".codeatrium" / "memory.db").exists()


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


# ---- invalid input handling (re-prompt) ----


def test_init_prompt_invalid_choice_reprompts(tmp_path, monkeypatch):
    """_resolve_skip_count で無効入力 → 再プロンプト → 有効値で続行"""
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=1, exchanges_per_file=3)

    # min_chars [1]=50 → skip_count "99"(無効) → 再入力 [1]=Skip all
    result = runner.invoke(app, ["init"], input="1\n99\n1\n")
    assert result.exit_code == 0
    assert "Invalid choice" in result.output

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    skipped = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at = 'skipped'"
    ).fetchone()[0]
    null_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NULL"
    ).fetchone()[0]
    con.close()
    # 無効入力で暴発せず、正しく全件 skipped になっている
    assert skipped > 0
    assert null_count == 0


def test_init_distill_now_accepts_n_alias(tmp_path, monkeypatch):
    """_ask_run_distill_now が 'n' を No として受け付ける"""
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=1, exchanges_per_file=3)

    # min_chars [1]=50 → skip [3]=全件蒸留 → distill now "n"=No
    result = runner.invoke(app, ["init"], input="1\n3\nn\n")
    assert result.exit_code == 0
    # "n" が受理されたので再プロンプトは出ない & 蒸留も走らない
    assert "Invalid choice. Please enter 1/2/y/n" not in result.output
    assert "Running distillation" not in result.output


def test_init_custom_count_out_of_range_reprompts(tmp_path, monkeypatch):
    """Custom 件数プロンプトで範囲外 → 再入力 → 有効値で続行"""
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=1, exchanges_per_file=3)

    # min_chars [1]=50 → skip [4]=custom → "0"(範囲外) → "2"(有効) → priority [1]=recent → distill now "n"
    result = runner.invoke(app, ["init"], input="1\n4\n0\n2\n1\nn\n")
    assert result.exit_code == 0
    assert "Must be ≥ 1" in result.output

    db = tmp_path / ".codeatrium" / "memory.db"
    con = get_connection(db)
    null_count = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NULL"
    ).fetchone()[0]
    con.close()
    # 最終的に 2 件が蒸留対象になっている
    assert null_count == 2


def test_init_custom_count_over_total_reprompts(tmp_path, monkeypatch):
    """Custom 件数プロンプトで total 超え → 再入力"""
    _setup_project_with_sessions(tmp_path, monkeypatch, num_files=1, exchanges_per_file=3)

    # total=3 なのに 99 を指定 → 再入力 → 3 で全件蒸留扱い
    result = runner.invoke(app, ["init"], input="1\n4\n99\n3\nn\n")
    assert result.exit_code == 0
    assert "Must be ≤ 3" in result.output


# ---- execution-phase cleanup ----


def test_init_cleanup_on_execution_failure(tmp_path, monkeypatch):
    """実行フェーズで例外が出たら .codeatrium/ がクリーンアップされる"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    def _boom(_root):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("codeatrium.cli.prime_cmd.inject_claude_md", _boom)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert "init failed" in result.output
    assert "simulated failure" in result.output
    # 部分状態が掃除されている
    assert not (tmp_path / ".codeatrium").exists()


def test_init_cleanup_on_keyboard_interrupt(tmp_path, monkeypatch):
    """Ctrl-C で .codeatrium/ がクリーンアップされる"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    def _interrupt(_root):
        raise KeyboardInterrupt

    monkeypatch.setattr("codeatrium.cli.prime_cmd.inject_claude_md", _interrupt)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 130
    assert "Interrupted" in result.output
    assert not (tmp_path / ".codeatrium").exists()


def test_init_per_file_index_error_continues(tmp_path, monkeypatch):
    """1ファイルの index_file 失敗で init 全体は落ちず、DB は残る"""
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=2, exchanges_per_file=3
    )

    import codeatrium.indexer as idx_mod

    original = idx_mod.index_file
    call_count = {"n": 0}

    def _flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("flaky fs error")
        return original(*args, **kwargs)

    monkeypatch.setattr("codeatrium.indexer.index_file", _flaky)

    # --skip-existing でも min_chars プロンプトは出る → "1"=50
    result = runner.invoke(app, ["init", "--skip-existing"], input="1\n")
    assert result.exit_code == 0
    assert "flaky fs error" in result.output
    # 2ファイル目は成功し DB は残っている
    assert (tmp_path / ".codeatrium" / "memory.db").exists()


# ---- hook auto-install ----


def test_init_installs_hooks_by_default(tmp_path, monkeypatch):
    """init 完了時に install_hooks が呼ばれる"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    calls = []

    def _spy(batch_limit: int = 20):
        calls.append(batch_limit)
        return True, "Installed hooks."

    monkeypatch.setattr("codeatrium.hooks.install_hooks", _spy)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert len(calls) == 1
    assert "Installed hooks." in result.output


def test_init_no_hooks_flag_skips_install(tmp_path, monkeypatch):
    """--no-hooks で install_hooks が呼ばれない"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    calls = []

    def _spy(batch_limit: int = 20):
        calls.append(batch_limit)
        return True, "Installed hooks."

    monkeypatch.setattr("codeatrium.hooks.install_hooks", _spy)

    result = runner.invoke(app, ["init", "--no-hooks"])
    assert result.exit_code == 0
    assert calls == []
    assert "Installed hooks." not in result.output


def test_init_hook_install_failure_warns_but_succeeds(tmp_path, monkeypatch):
    """install_hooks が例外を投げても init は成功する（警告のみ）"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    def _boom(batch_limit: int = 20):
        raise RuntimeError("permission denied")

    monkeypatch.setattr("codeatrium.hooks.install_hooks", _boom)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0  # init 自体は成功
    assert "Hook install failed" in result.output
    assert "permission denied" in result.output
    # DB は残っている
    assert (tmp_path / ".codeatrium" / "memory.db").exists()


# ---- EmbedderSetupError 環境エラーの友好的ハンドリング ----


def test_embedder_setup_error_wraps_import_failure(monkeypatch):
    """sentence_transformers ロード失敗が EmbedderSetupError に包まれる"""
    import sys
    import types

    from codeatrium.embedder import Embedder, EmbedderSetupError

    fake = types.ModuleType("sentence_transformers")

    class _Broken:
        def __init__(self, *args, **kwargs):
            raise AttributeError("_ARRAY_API not found")

    fake.SentenceTransformer = _Broken  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    # ソケットを無効化して必ず直接ロード経路に入れる
    monkeypatch.setenv("CODEATRIUM_NO_SOCK", "1")

    with pytest.raises(EmbedderSetupError, match="numpy"):
        Embedder().embed_passage("test")


def test_init_distill_embedder_setup_error_friendly_message(tmp_path, monkeypatch):
    """init 中の蒸留で EmbedderSetupError が出たら友好的メッセージが出る"""
    _setup_project_with_sessions(
        tmp_path, monkeypatch, num_files=1, exchanges_per_file=3
    )

    from codeatrium.embedder import EmbedderSetupError

    def _raising_distill_all(*_args, **_kwargs):
        raise EmbedderSetupError(
            "Embedding model failed to load: _ARRAY_API not found\n"
            "  Likely cause: numpy / pyarrow binary incompatibility.\n"
            "  Fix: pip install 'numpy<2'  or  pip install -U pyarrow"
        )

    monkeypatch.setattr("codeatrium.distiller.distill_all", _raising_distill_all)

    # min_chars=50, Distill all, Yes-run-now
    result = runner.invoke(app, ["init"], input="1\n3\ny\n")
    assert result.exit_code == 1
    # 友好的メッセージ
    assert "Embedding model failed to load" in result.output
    assert "numpy<2" in result.output
    assert "loci distill" in result.output
    # DB はクリーンアップされず残っている（索引済み）
    assert (tmp_path / ".codeatrium" / "memory.db").exists()
