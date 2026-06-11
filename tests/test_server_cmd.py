"""loci server コマンドのテスト — 未初期化リポジトリでの暴発防止"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _make_initialized_repo(tmp_path: Path) -> Path:
    """db_path(root).exists() が True になる最小リポジトリを作る"""
    cdir = tmp_path / ".codeatrium"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "memory.db").touch()
    return tmp_path


def _fake_ok_socket() -> MagicMock:
    """ping に {"status":"ok"} を返す context-manager 対応の偽ソケット"""
    s = MagicMock()
    s.__enter__.return_value = s
    s.__exit__.return_value = False
    s.recv.return_value = b'{"status":"ok"}\n'
    return s


def test_server_start_already_running(tmp_path: Path, monkeypatch) -> None:
    """稼働中サーバーがいる状態で start を再実行しても二重起動しない（H3）"""
    _make_initialized_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    sock = tmp_path / ".codeatrium" / "embedder.sock"
    sock.touch()  # exists() を True にする

    popen = MagicMock()
    with patch("codeatrium.paths.git_root", return_value=None), \
        patch("socket.socket", return_value=_fake_ok_socket()), \
        patch("subprocess.Popen", popen):
        result = runner.invoke(app, ["server", "start"])

    assert "already running" in result.output
    popen.assert_not_called()


def test_server_start_stale_cleanup(tmp_path: Path, monkeypatch) -> None:
    """死亡 PID の pid ファイルが残っていても os.kill 生存確認で掃除して起動する（H3）"""
    _make_initialized_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    cdir = tmp_path / ".codeatrium"
    pid_file = cdir / "embedder.pid"
    pid_file.write_text("999999999")  # 存在しない PID（socket ファイルは作らない）

    popen = MagicMock()
    popen.return_value.pid = 12345

    # socket が無いので ping はスキップされ pid 生存確認パスを通る。
    # Popen 後の wait ループを抜けるため time.sleep を無効化する。
    with patch("codeatrium.paths.git_root", return_value=None), \
        patch("subprocess.Popen", popen), \
        patch("time.sleep", lambda *a, **k: None):
        runner.invoke(app, ["server", "start"])

    # 死亡 PID は os.kill(pid, 0) の ProcessLookupError で除去され、
    # 新サーバーが起動して新しい PID が pid ファイルに書かれる
    assert popen.called
    assert pid_file.read_text().strip() == "12345"
