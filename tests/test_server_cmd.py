"""loci server コマンドのテスト — 未初期化リポジトリでの暴発防止"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app

runner = CliRunner()


def test_server_start_rejects_uninitialized_repo(tmp_path: Path, monkeypatch) -> None:
    """loci init していないリポジトリで loci server start を実行するとエラーになる"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["server", "start"])
    assert result.exit_code != 0
    assert "loci init" in result.output
    # .codeatrium ディレクトリが作成されていないこと
    assert not (tmp_path / ".codeatrium").exists()
