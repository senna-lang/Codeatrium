"""
embedder_server._handle_client のテスト（H5 ソケット堅牢性 / Q7 テスト空白埋め）

実モデルはロードせず embedder は MagicMock。接続は socket.socketpair() を使う。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import numpy as np
import pytest

from codeatrium.embedder_server import _handle_client


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
