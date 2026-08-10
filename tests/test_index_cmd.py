"""loci index コマンドのテスト — 未初期化リポジトリでの DB 自動生成防止"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db

runner = CliRunner()


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
