"""loci server start/stop/status コマンド"""

from __future__ import annotations

import typer

server_app = typer.Typer(help="embedding サーバー管理")


@server_app.command("start")
def server_start() -> None:
    """embedding サーバーをバックグラウンドで起動する"""
    import json as _json
    import socket as _socket
    import subprocess

    from codeatrium.embedder import _loci_python
    from codeatrium.paths import find_project_root, server_pid_path, sock_path

    root = find_project_root()
    sock = sock_path(root)

    if sock.exists():
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(str(sock))
                s.sendall((_json.dumps({"type": "ping"}) + "\n").encode())
                resp = s.recv(256)
                if b"ok" in resp:
                    typer.echo("Server is already running.")
                    return
        except Exception:
            sock.unlink(missing_ok=True)

    pid_path = server_pid_path(root)
    proc = subprocess.Popen(
        [_loci_python(), "-m", "codeatrium.embedder_server", str(sock)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid))

    import time

    for i in range(150):
        if sock.exists():
            typer.echo(f"Server started (PID {proc.pid})")
            return
        time.sleep(0.2)
        if i % 25 == 24:
            typer.echo("  Loading model...", err=True)

    typer.echo("Server failed to start.", err=True)
    raise typer.Exit(1)


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
    """embedding サーバーの状態を確認する"""
    import json as _json
    import socket as _socket

    from codeatrium.paths import find_project_root, server_pid_path, sock_path

    root = find_project_root()
    sock = sock_path(root)

    if not sock.exists():
        typer.echo("Server: stopped")
        return

    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(sock))
            s.sendall((_json.dumps({"type": "ping"}) + "\n").encode())
            resp = s.recv(256)
            if b"ok" in resp:
                pid_path = server_pid_path(root)
                pid = pid_path.read_text().strip() if pid_path.exists() else "unknown"
                typer.echo(f"Server: running (PID {pid})")
                typer.echo(f"Socket: {sock}")
                return
    except Exception:
        pass

    typer.echo("Server: socket exists but not responding")
    sock.unlink(missing_ok=True)
