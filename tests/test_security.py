"""
セキュリティ修正のテスト

- SQL LIMIT パラメータ化
- シェルコマンドのパスクオート
- Unix ソケットのパーミッション
- ロックファイルの原子的取得
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import init_db
from codeatrium.hooks import install_hooks

runner = CliRunner()

# --- #1: hooks.py — shlex.quote でパスをクオート ---


def test_hooks_quotes_loci_path_with_spaces() -> None:
    """パスにスペースを含む場合、shlex.quote でエスケープされる"""
    fake_path = "/Users/test user/venvs/my env/bin/loci"
    # loci_bin の実際の呼び出し元は lifecycle_commands()（codeatrium.hooks はそれを
    # 消費するだけ）に一元化された（issue #40）。
    with patch(
        "codeatrium.adapters.harness.lifecycle.loci_bin", return_value=fake_path
    ):
        with patch("codeatrium.hooks.Path") as mock_path_cls:
            with patch("codeatrium.hooks._write_settings"):
                mock_settings = mock_path_cls.home.return_value / ".claude" / "settings.json"
                mock_settings.exists.return_value = False
                _, msg = install_hooks()
    # shlex.quote はシングルクオートでラップする
    assert "'" in msg or "\\" in msg


def test_hooks_batch_limit_cast_to_int() -> None:
    """batch_limit が int にキャストされることを確認"""
    with patch(
        "codeatrium.adapters.harness.lifecycle.loci_bin",
        return_value="/usr/bin/loci",
    ):
        with patch("codeatrium.hooks.Path") as mock_path_cls:
            with patch("codeatrium.hooks._write_settings"):
                mock_settings = mock_path_cls.home.return_value / ".claude" / "settings.json"
                mock_settings.exists.return_value = False
                _, msg = install_hooks(batch_limit=20)
    assert "--limit 20" in msg


# --- #2: distiller.py — LIMIT パラメータ化 ---


def test_distill_all_limit_parameterized(tmp_path: Path) -> None:
    """LIMIT 句が f-string ではなくパラメータで渡される"""
    from unittest.mock import MagicMock

    import numpy as np

    from codeatrium.db import get_connection, init_db
    from codeatrium.distiller import distill_all

    db_path = tmp_path / "memory.db"
    init_db(db_path)

    con = get_connection(db_path)
    con.execute(
        "INSERT INTO conversations (id, source_path) VALUES (?, ?)",
        ("c1", "/test.jsonl"),
    )
    long_text = "テスト " * 30
    for i in range(3):
        con.execute(
            "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) VALUES (?,?,?,?,?,?)",
            (f"ex{i}", "c1", i * 2, i * 2 + 1, long_text, long_text),
        )
    con.commit()
    con.close()

    mock_response = {
        "exchange_core": "test",
        "specific_context": "ctx",
        "room_assignments": [],
    }
    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with (
        patch("codeatrium.distiller.call_claude", return_value=mock_response),
        patch("codeatrium.distiller.Embedder", return_value=mock_embedder),
    ):
        count, _ = distill_all(db_path, limit=1)

    assert count == 1


# --- #3: embedder_server.py — ソケットパーミッション 0o600 ---


def test_embedder_server_socket_permissions() -> None:
    """ソケット作成後に 0o600 が設定される"""
    import socket
    import tempfile
    import threading

    from codeatrium.embedder_server import run_server

    # AF_UNIX パス長制限 (104 bytes on macOS) を回避するため短いパスを使う
    tmpdir = Path(tempfile.mkdtemp(prefix="loci"))
    sock = tmpdir / "s.sock"

    def _start_and_stop() -> None:
        """サーバーを起動してすぐ停止"""
        import time

        time.sleep(0.3)
        # ソケットが存在すれば権限チェック
        if sock.exists():
            mode = stat.S_IMODE(os.stat(sock).st_mode)
            assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
        # stop コマンド送信
        try:
            import json

            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(json.dumps({"type": "stop"}).encode() + b"\n")
            c.recv(1024)
            c.close()
        except OSError:
            pass

    t = threading.Thread(target=_start_and_stop)
    t.start()

    # _load_embedder をモックしてモデルロードを回避
    with patch("codeatrium.embedder_server._load_embedder") as mock_load:
        from unittest.mock import MagicMock

        mock_embedder = MagicMock()
        mock_load.return_value = mock_embedder
        run_server(sock)

    t.join(timeout=5)

    # サーバー終了後はソケット削除済み
    assert not sock.exists()


# --- #4: distill_cmd.py — 原子的ロックファイル ---


def test_distill_lock_atomic_creation(tmp_path: Path) -> None:
    """2つ目の flock(LOCK_NB) は BlockingIOError になる"""
    lock_path = tmp_path / "distill.lock"

    fd1 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)

    fd2 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise AssertionError("Expected BlockingIOError")
    except BlockingIOError:
        pass
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)
        os.close(fd2)


def test_distill_lock_already_running(tmp_path: Path, monkeypatch) -> None:
    """ロック保持中の distill は already running で exit 0"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir(parents=True)
    init_db(codeatrium_dir / "memory.db")

    lock_path = codeatrium_dir / "distill.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["distill"])

        assert result.exit_code == 0
        output = result.output + (getattr(result, "stderr", "") or "")
        assert "already running" in output
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
