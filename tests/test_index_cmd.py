"""loci index コマンドのテスト — 未初期化リポジトリでの DB 自動生成防止"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import init_db

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
