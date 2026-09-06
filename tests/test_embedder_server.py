"""
embedder_server._handle_client / run_server のテスト（H5 ソケット堅牢性 / Q7 テスト空白埋め / issue #16）

実モデルはロードせず embedder は MagicMock。接続は socket.socketpair() を使う。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from codeatrium.embedder_server import _handle_client, ping_server, run_server


def _read_line(sock: socket.socket) -> dict:
    """改行が現れるまで recv して JSON を 1 行パースする"""
    buf = b""
    sock.settimeout(2.0)
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.split(b"\n")[0])


def _start_handler(
    server_conn: socket.socket,
    embedder: object,
    stop_event: threading.Event,
) -> threading.Thread:
    """_handle_client を daemon スレッドで起動する"""
    t = threading.Thread(
        target=_handle_client,
        args=(server_conn, embedder, [0.0], stop_event),
        daemon=True,
    )
    t.start()
    return t


def test_handle_client_chunked_request() -> None:
    """リクエスト JSON が複数 recv に分割到着しても 1 リクエストとして処理される"""
    server_conn, client_conn = socket.socketpair()
    embedder = type("E", (), {})()
    embedder.embed = lambda text: np.array([0.1, 0.2, 0.3], dtype=np.float32)  # type: ignore[attr-defined]
    stop_event = threading.Event()
    t = _start_handler(server_conn, embedder, stop_event)
    try:
        client_conn.sendall(b'{"type":"query","te')
        time.sleep(0.05)
        client_conn.sendall(b'xt":"hello"}\n')
        resp = _read_line(client_conn)
        assert resp["embedding"] == pytest.approx([0.1, 0.2, 0.3])
    finally:
        client_conn.close()
        server_conn.close()
        t.join(timeout=2.0)


def test_handle_client_invalid_json_then_valid() -> None:
    """不正 JSON は error 応答を返し、接続を切らず後続を処理する"""
    server_conn, client_conn = socket.socketpair()
    embedder = type("E", (), {})()
    embedder.embed = lambda text: np.array([1.0], dtype=np.float32)  # type: ignore[attr-defined]
    stop_event = threading.Event()
    t = _start_handler(server_conn, embedder, stop_event)
    try:
        client_conn.sendall(b"not valid json\n")
        first = _read_line(client_conn)
        assert first["error"] == "invalid json"
        client_conn.sendall(b'{"type":"query","text":"hi"}\n')
        second = _read_line(client_conn)
        assert second["embedding"] == [1.0]
    finally:
        client_conn.close()
        server_conn.close()
        t.join(timeout=2.0)


def test_handle_client_ping() -> None:
    """ping に status ok を返す"""
    server_conn, client_conn = socket.socketpair()
    embedder = type("E", (), {})()
    stop_event = threading.Event()
    t = _start_handler(server_conn, embedder, stop_event)
    try:
        client_conn.sendall(b'{"type":"ping"}\n')
        resp = _read_line(client_conn)
        assert resp == {"status": "ok"}
    finally:
        client_conn.close()
        server_conn.close()
        t.join(timeout=2.0)


def test_handle_client_stop() -> None:
    """stop で stopping を返し stop_event がセットされる"""
    server_conn, client_conn = socket.socketpair()
    embedder = type("E", (), {})()
    stop_event = threading.Event()
    t = _start_handler(server_conn, embedder, stop_event)
    try:
        client_conn.sendall(b'{"type":"stop"}\n')
        resp = _read_line(client_conn)
        assert resp == {"status": "stopping"}
        t.join(timeout=2.0)
        assert stop_event.is_set()
    finally:
        client_conn.close()
        server_conn.close()



def test_run_server_refuses_to_clobber_live_socket(monkeypatch) -> None:
    """bind 前に既存ソケットへ ping し、生存応答があれば奪わず終了する（issue #16）。

    生存中の別プロセスを模した実 Unix ソケットを先に bind しておき、そこへ
    run_server(sock) を呼んでも: (1) 自プロセスはモデルをロードしない
    (2) 既存のソケットファイルを unlink/rebind しない、ことを確認する。

    AF_UNIX のパス長制限（~104バイト）を避けるため、pytest の tmp_path ではなく
    /tmp 直下の短い一時ディレクトリを使う。
    """
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    sock = tmp_dir / "e.sock"
    try:
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(str(sock))
        live.listen(1)
        live_inode = os.stat(sock).st_ino

        def serve_one() -> None:
            try:
                live.settimeout(3.0)
                conn, _ = live.accept()
                data = conn.recv(4096)
                if b"ping" in data:
                    conn.sendall(b'{"status": "ok"}\n')
                conn.close()
            except OSError:
                pass

        responder = threading.Thread(target=serve_one, daemon=True)
        responder.start()

        load_calls = {"n": 0}

        def fake_load_embedder():
            load_calls["n"] += 1
            return MagicMock()

        import codeatrium.embedder_server as es_module

        monkeypatch.setattr(es_module, "_load_embedder", fake_load_embedder)

        worker = threading.Thread(target=run_server, args=(sock,), daemon=True)
        worker.start()
        worker.join(timeout=2.0)
        responder.join(timeout=2.0)
        live.close()

        assert not worker.is_alive(), (
            "run_server must return immediately, not enter the serve loop"
        )
        assert load_calls["n"] == 0, "must not load the model when an existing server is alive"
        assert sock.exists()
        assert os.stat(sock).st_ino == live_inode, (
            "must not unlink/rebind the live process's socket"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_ping_server_false_when_no_socket_file(tmp_path: Path) -> None:
    """ソケットファイルが存在しなければ ping_server は接続を試みず False を返す"""
    assert ping_server(tmp_path / "no-such.sock") is False


def test_run_server_idle_timeout_removes_pid_file(monkeypatch) -> None:
    """idle timeout での自動終了時に、ソケットだけでなく PID ファイルも削除する（issue #16）。

    AF_UNIX のパス長制限を避けるため /tmp 直下の短い一時ディレクトリを使う。
    """
    import shutil
    import tempfile

    import codeatrium.embedder_server as es_module

    tmp_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    sock = tmp_dir / "e.sock"
    pid_file = tmp_dir / "embedder.pid"
    pid_file.write_text("999999")

    monkeypatch.setattr(es_module, "_load_embedder", lambda: MagicMock())
    monkeypatch.setattr(es_module, "IDLE_TIMEOUT", 0)  # ループ1周目で即 idle 判定させる

    try:
        run_server(sock)
        assert not sock.exists()
        assert not pid_file.exists()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)