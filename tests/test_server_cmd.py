"""loci server コマンドのテスト — 未初期化リポジトリでの暴発防止"""

from __future__ import annotations

import threading
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



def test_server_start_concurrent_no_double_spawn(tmp_path: Path, monkeypatch) -> None:
    """並行 `loci server start` が同一ソケットを二重 spawn しない（issue #16 多重起動レース）。

    server_cmd.py の `if sock.exists()` チェックを 2 スレッドが同時に素通りしてから
    それぞれ Popen する、という issue の説明どおりのレースウィンドウを
    `Path.exists` へのバリアで強制的に発生させる。
    """
    from codeatrium.cli.server_cmd import server_start

    _make_initialized_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    sock = tmp_path / ".codeatrium" / "embedder.sock"

    popen_lock = threading.Lock()
    spawned_pids: list[int] = []

    def fake_popen(*args, **kwargs):
        # 実際の embedder_server 起動（ソケット bind 完了）を模す
        with popen_lock:
            pid = 20000 + len(spawned_pids)
            spawned_pids.append(pid)
        sock.touch()
        m = MagicMock()
        m.pid = pid
        return m

    def fake_socket_factory(*_args, **_kwargs):
        if sock.exists():
            return _fake_ok_socket()
        raise ConnectionRefusedError()

    # sock.exists() 呼び出しを 2 者が揃うまで足止めし、
    # 「両方が未起動と判定してから spawn する」レースを毎回確実に起こす。
    # ロックで直列化された修正後は、片方が待機中にもう片方が来ないため
    # タイムアウトで素通りするだけになる（＝レース自体が起きない）。
    barrier = threading.Barrier(2)
    orig_exists = Path.exists

    def racy_exists(self: Path) -> bool:
        if self.name == "embedder.sock":
            try:
                barrier.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
        return orig_exists(self)

    errors: list[BaseException] = []

    def run() -> None:
        try:
            server_start()
        except Exception as e:  # typer.Exit を含め、テスト側で記録して後で検査する
            errors.append(e)

    with patch("codeatrium.paths.git_root", return_value=None), \
        patch("subprocess.Popen", side_effect=fake_popen), \
        patch("socket.socket", side_effect=fake_socket_factory), \
        patch("time.sleep", lambda *a, **k: None), \
        patch.object(Path, "exists", racy_exists):
        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert len(spawned_pids) == 1, f"expected exactly one spawn, got {spawned_pids}"


def test_server_status_never_deletes_socket(tmp_path: Path, monkeypatch) -> None:
    """status は read-only: ping が無応答でも稼働中ソケットを削除しない（issue #16）。"""
    _make_initialized_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    sock = tmp_path / ".codeatrium" / "embedder.sock"
    sock.touch()

    def fake_socket_factory(*_args, **_kwargs):
        raise TimeoutError("busy: no reply within timeout")

    with patch("codeatrium.paths.git_root", return_value=None), \
        patch("socket.socket", side_effect=fake_socket_factory):
        result = runner.invoke(app, ["server", "status"])

    assert "not responding" in result.output
    assert sock.exists(), "status must never unlink the socket (it is read-only)"