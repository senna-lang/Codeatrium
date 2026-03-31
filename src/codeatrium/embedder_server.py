"""
embedder_server.py — multilingual-e5-small を常駐させる Unix ソケットサーバー

プロトコル:
  request (改行区切り JSON):  {"type": "query"|"passage", "text": "..."}
  response (改行区切り JSON): {"embedding": [0.1, 0.2, ...]}
  特殊コマンド:               {"type": "ping"} → {"status": "ok"}
                              {"type": "stop"} → サーバー終了

ライフサイクル:
  - loci server start でバックグラウンド起動
  - IDLE_TIMEOUT 秒間リクエストなし → 自動終了
  - loci server stop / SIGTERM で即終了
  - ソケットファイルは .codeatrium/embedder.sock
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeatrium.embedder import Embedder

IDLE_TIMEOUT = 600  # 10分間無リクエストで自動終了
BACKLOG = 8
RECV_BUFSIZE = 65536


def _load_embedder() -> Embedder:
    # サーバー内では直接モデルを使う（ソケット経由にすると自己接続デッドロック）

    os.environ["CODEATRIUM_NO_SOCK"] = "1"
    from codeatrium.embedder import Embedder

    embedder = Embedder()
    del os.environ["CODEATRIUM_NO_SOCK"]
    return embedder


def _handle_client(
    conn: socket.socket,
    embedder: Embedder,
    last_activity: list[float],
    stop_event: threading.Event,
) -> None:
    """1クライアント接続を処理する"""
    try:
        buf = b""
        while True:
            chunk = conn.recv(RECV_BUFSIZE)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    conn.sendall(json.dumps({"error": "invalid json"}).encode() + b"\n")
                    continue

                req_type = req.get("type", "")

                if req_type == "ping":
                    conn.sendall(json.dumps({"status": "ok"}).encode() + b"\n")
                    last_activity[0] = time.monotonic()
                    continue

                if req_type == "stop":
                    conn.sendall(json.dumps({"status": "stopping"}).encode() + b"\n")
                    stop_event.set()
                    return

                text = req.get("text", "")
                if not text:
                    conn.sendall(json.dumps({"error": "missing text"}).encode() + b"\n")
                    continue

                if req_type == "query":
                    vec = embedder.embed(text)
                elif req_type == "passage":
                    vec = embedder.embed_passage(text)
                else:
                    conn.sendall(
                        json.dumps({"error": f"unknown type: {req_type}"}).encode()
                        + b"\n"
                    )
                    continue

                resp = json.dumps({"embedding": vec.tolist()})
                conn.sendall(resp.encode() + b"\n")
                last_activity[0] = time.monotonic()
    except (OSError, BrokenPipeError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run_server(sock_path: Path) -> None:
    """ソケットサーバーを起動してリクエストを処理する（ブロッキング）"""
    # 既存ソケットファイルを削除
    sock_path.unlink(missing_ok=True)

    embedder = _load_embedder()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(BACKLOG)
    server.settimeout(1.0)  # accept の timeout（idle チェック用）

    last_activity: list[float] = [time.monotonic()]
    stop_event = threading.Event()

    def _sigterm_handler(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    try:
        while not stop_event.is_set():
            # idle timeout チェック
            if time.monotonic() - last_activity[0] > IDLE_TIMEOUT:
                break

            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            # クライアントごとにスレッド
            t = threading.Thread(
                target=_handle_client,
                args=(conn, embedder, last_activity, stop_event),
                daemon=True,
            )
            t.start()
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)


def main() -> None:
    """CLI エントリポイント: python -m codeatrium.embedder_server <sock_path>"""
    if len(sys.argv) < 2:
        print("Usage: python -m codeatrium.embedder_server <sock_path>", file=sys.stderr)
        sys.exit(1)
    run_server(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
