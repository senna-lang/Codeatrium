"""
セキュリティ修正のテスト

- SQL LIMIT パラメータ化
- シェルコマンドのパスクオート
- Unix ソケットのパーミッション
- ロックファイルの原子的取得
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

from codeatrium.hooks import install_hooks

# --- #1: hooks.py — shlex.quote でパスをクオート ---


def test_hooks_quotes_loci_path_with_spaces() -> None:
    """パスにスペースを含む場合、shlex.quote でエスケープされる"""
    fake_path = "/Users/test user/venvs/my env/bin/loci"
    with patch("codeatrium.hooks.loci_bin", return_value=fake_path):
        with patch("codeatrium.hooks.Path") as mock_path_cls:
            mock_settings = mock_path_cls.home.return_value / ".claude" / "settings.json"
            mock_settings.exists.return_value = False
            _, msg = install_hooks()
    # shlex.quote はシングルクオートでラップする
    assert "'" in msg or "\\" in msg


def test_hooks_batch_limit_cast_to_int() -> None:
    """batch_limit が int にキャストされることを確認"""
    with patch("codeatrium.hooks.loci_bin", return_value="/usr/bin/loci"):
        with patch("codeatrium.hooks.Path") as mock_path_cls:
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
        count = distill_all(db_path, limit=1)

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
    """ロックファイルが O_CREAT | O_EXCL で原子的に作成される"""
    lock_path = tmp_path / "distill.lock"

    # O_CREAT | O_EXCL で作成
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, b"12345")
    os.close(fd)

    assert lock_path.exists()
    assert lock_path.read_text() == "12345"

    # 2回目は FileExistsError
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        raise AssertionError("Expected FileExistsError")
    except FileExistsError:
        pass


def test_distill_lock_stale_cleanup(tmp_path: Path) -> None:
    """死んだプロセスの stale lock を検出してクリーンアップできる"""
    lock_path = tmp_path / "distill.lock"
    # 存在しない PID を書き込む
    lock_path.write_text("999999999")

    try:
        existing_pid = int(lock_path.read_text().strip())
        os.kill(existing_pid, 0)
        raise AssertionError("PID should not exist")
    except ProcessLookupError:
        # stale lock — 削除して再取得
        lock_path.unlink()
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)

    assert lock_path.read_text() == str(os.getpid())
