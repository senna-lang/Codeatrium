"""loci server start/stop/status コマンド"""

from __future__ import annotations

import typer

server_app = typer.Typer(help="embedding サーバー管理")

_SERVER_STARTUP_POLL_ATTEMPTS: int = 150  # サーバー起動確認のポーリング回数（0.2秒 × 150 = 最大30秒待機）


@server_app.command("start")
def server_start() -> None:
    """embedding サーバーをバックグラウンドで起動する"""
    import fcntl
    import os
    import subprocess
    import time

    from codeatrium.embedder import _loci_python
    from codeatrium.embedder_server import ping_server
    from codeatrium.paths import db_path, find_project_root, server_pid_path, sock_path

    root = find_project_root()
    if not db_path(root).exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    sock = sock_path(root)
    pid_path = server_pid_path(root)
    lock_path = sock.parent / "server.lock"

    # 多重起動レース防止（issue #16）: 「稼働確認 → spawn → 起動待機」の全体を
    # プロセス間排他ロックで直列化する。起動待機の完了までロックを保持するのは、
    # 保持を打ち切ると後続プロセスが「ソケットまだ無し＝未起動」と誤判定し、
    # ロード中のサーバーへ追い打ちで二重 spawn してしまうため。
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        if sock.exists():
            if ping_server(sock):
                typer.echo("Server is already running.")
                return
            sock.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)

        if pid_path.exists():
            try:
                _pid = int(pid_path.read_text().strip())
                os.kill(_pid, 0)
            except (ProcessLookupError, ValueError):
                # プロセス不在 or 不正な PID → stale とみなして除去
                pid_path.unlink(missing_ok=True)
            except PermissionError:
                # 別ユーザーのプロセスが生存 → 触らない
                pass

        proc = subprocess.Popen(
            [_loci_python(), "-m", "codeatrium.embedder_server", str(sock)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_path.write_text(str(proc.pid))

        for i in range(_SERVER_STARTUP_POLL_ATTEMPTS):
            if sock.exists():
                typer.echo(f"Server started (PID {proc.pid})")
                return
            time.sleep(0.2)
            if i % 25 == 24:
                typer.echo("  Loading model...", err=True)

        typer.echo("Server failed to start.", err=True)
        raise typer.Exit(1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@server_app.command("stop")
def server_stop() -> None:
    """embedding サーバーを停止する"""
    import json as _json
    import socket as _socket

    from codeatrium.paths import find_project_root, server_pid_path, sock_path

    root = find_project_root()
    sock = sock_path(root)

    if not sock.exists():
        typer.echo("Server is not running.")
        return

    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(sock))
            s.sendall((_json.dumps({"type": "stop"}) + "\n").encode())
        typer.echo("Server stopped.")
    except Exception as e:
        typer.echo(f"Could not connect to server: {e}", err=True)
        sock.unlink(missing_ok=True)

    server_pid_path(root).unlink(missing_ok=True)


@server_app.command("status")
def server_status() -> None:
    """embedding サーバーの状態を確認する（read-only — 稼働中/応答なしを問わずソケットを削除しない）"""
    from codeatrium.embedder_server import ping_server
    from codeatrium.paths import find_project_root, server_pid_path, sock_path

    root = find_project_root()
    sock = sock_path(root)

    if not sock.exists():
        typer.echo("Server: stopped")
        return

    if ping_server(sock):
        pid_path = server_pid_path(root)
        pid = pid_path.read_text().strip() if pid_path.exists() else "unknown"
        typer.echo(f"Server: running (PID {pid})")
        typer.echo(f"Socket: {sock}")
        return

    # ping 無応答でも削除しない: 全ワーカースレッドが busy で一時的にタイムアウト
    # しているだけの可能性があり、ここで unlink すると稼働中サーバーの
    # ソケットを奪い、後続の呼び出し元がコールドスタート二重起動してしまう（issue #16）。
    typer.echo("Server: socket exists but not responding")
