"""
loci CLI エントリポイント

コマンド:
  loci init         - .codeatrium/memory.db を初期化
  loci index        - ~/.claude/projects/ の .jsonl を処理
  loci distill      - 未蒸留 exchange を claude -p で palace object に変換
  loci search       - BM25(V) + HNSW(D) RRF セマンティック検索
  loci context      - シンボル名から過去会話を逆引き
  loci status       - インデックス状態を表示
  loci hook install - Claude Code Stop hook を ~/.claude/settings.json に登録
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, cast

import typer

app = typer.Typer(help="CLI-first memory layer for AI coding agents")

# デフォルトの Claude Code セッションログ格納先
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEATRIUM_DIR = ".codeatrium"
DB_NAME = "memory.db"


def _git_root() -> Path | None:
    """git rev-parse --show-toplevel でリポジトリルートを返す。git 外なら None"""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        return None


def _find_project_root() -> Path:
    """.codeatrium/ を探してプロジェクトルートを返す。
    検索順: cwd → 親ディレクトリ（git root まで）
    git root を超えて遡らないことでプロジェクト外の DB を拾わない。
    """
    cwd = Path.cwd()
    git_root = _git_root()
    # 探索上限: git root があればそこまで、なければ cwd のみ
    candidates = [cwd, *cwd.parents]
    for p in candidates:
        if (p / CODEATRIUM_DIR).exists():
            return p
        if git_root and p == git_root:
            break
    # 見つからなければ git root（なければ cwd）を返す
    return git_root or cwd


def _db_path(project_root: Path) -> Path:
    return project_root / CODEATRIUM_DIR / DB_NAME


def _resolve_claude_projects_path(project_root: Path) -> Path | None:
    """project_root から対応する ~/.claude/projects/<hash>/ を解決する。
    Claude Code はパスの "/" を "-" に変換したディレクトリ名を使う。
    project_root と cwd の両方を試す。
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    candidates = [project_root, Path.cwd()]
    for base in candidates:
        dir_name = str(base).replace("/", "-")
        candidate = CLAUDE_PROJECTS_DIR / dir_name
        if candidate.exists() and any(candidate.rglob("*.jsonl")):
            return candidate
    return None


# ---- init ----


@app.command()
def init() -> None:
    """プロジェクトルートに .codeatrium/memory.db を初期化する"""
    from codeatrium.db import init_db

    root = _find_project_root()
    db = _db_path(root)

    if db.exists():
        typer.echo(f"Already initialized: {db}")
        return

    init_db(db)
    typer.echo(f"Initialized: {db}")


# ---- index ----


@app.command()
def index(
    path: Annotated[
        Path | None, typer.Option(help="インデックス対象ディレクトリ")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """未処理の .jsonl を処理して exchanges テーブルに登録する（FTS5 自動同期）"""
    from codeatrium.db import get_connection, init_db
    from codeatrium.indexer import index_file

    root = _find_project_root()
    db = _db_path(root)
    init_db(db)

    # 対象ディレクトリを決定
    target_dir = path or _resolve_claude_projects_path(root)
    if target_dir is None:
        typer.echo("Claude projects dir not found. Use --path to specify.", err=True)
        raise typer.Exit(1)

    jsonl_files = list(target_dir.rglob("*.jsonl"))
    if not jsonl_files:
        typer.echo("No .jsonl files found.")
        return

    # 登録済み source_path を取得
    con = get_connection(db)
    indexed = {
        row[0]
        for row in con.execute("SELECT source_path FROM conversations").fetchall()
    }
    con.close()

    new_files = [f for f in jsonl_files if str(f) not in indexed]
    if not new_files:
        typer.echo("Nothing new to index.")
        return

    total_exchanges = 0
    for jsonl in new_files:
        count = index_file(jsonl, db)
        if count == 0:
            continue
        if verbose:
            typer.echo(f"  {jsonl.name}: {count} exchanges")
        total_exchanges += count

    typer.echo(f"Indexed {len(new_files)} file(s), {total_exchanges} exchange(s).")


# ---- distill ----


@app.command()
def distill(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", help="処理する最大件数（省略時は全件）"),
    ] = None,
) -> None:
    """未蒸留の exchange を claude -p で蒸留して palace_objects を生成する"""
    import os

    from codeatrium.distiller import distill_all

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    # プロセスレベルのロック：多重起動を防ぐ
    lock_path = db.parent / "distill.lock"
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            # PID が実際に生きているか確認
            os.kill(existing_pid, 0)
            typer.echo(
                f"loci distill is already running (PID {existing_pid}). Exiting.",
                err=True,
            )
            raise typer.Exit(0)
        except (ValueError, ProcessLookupError, PermissionError):
            # PID が死んでいる or 読めない → ロックファイルが残骸なので上書き
            pass

    lock_path.write_text(str(os.getpid()))
    try:
        count = distill_all(db, limit=limit)
        typer.echo(f"Distilled {count} exchange(s).")
    finally:
        lock_path.unlink(missing_ok=True)


# ---- search ----


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """BM25(V) + HNSW(D) RRF でクエリに近い過去会話を返す"""
    from codeatrium.embedder import Embedder
    from codeatrium.search import search_combined

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    embedder = Embedder()
    query_vec = embedder.embed(query)
    results = search_combined(db, query, query_vec, limit=limit)

    if not results:
        typer.echo("No results found.")
        return

    if json_output:
        output = [
            {
                "exchange_core": r.exchange_core,
                "specific_context": r.specific_context,
                "rooms": r.rooms,
                "symbols": r.symbols,
                "verbatim_ref": r.verbatim_ref,
            }
            for r in results
        ]
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(results, 1):
            typer.echo(f"\n[{i}] score={r.score:.4f}")
            if r.exchange_core:
                typer.echo(f"    {r.exchange_core}")
            for sym in r.symbols[:2]:
                typer.echo(f"    {sym['file']}:{sym['line']}  {sym['name']}")
            if r.verbatim_ref:
                typer.echo(f"    {r.verbatim_ref}")


# ---- context ----


@app.command()
def context(
    symbol: Annotated[
        str, typer.Option("--symbol", "-s", help="シンボル名（部分一致）")
    ],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """シンボル名から関連する過去会話を逆引きする"""
    from codeatrium.db import get_connection

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    rows = con.execute(
        """
        SELECT
            s.symbol_name,
            s.symbol_kind,
            s.file_path,
            s.signature,
            s.line,
            e.id        AS exchange_id,
            e.user_content,
            e.agent_content,
            p.exchange_core,
            p.specific_context
        FROM symbols s
        JOIN palace_objects p ON p.id = s.palace_object_id
        JOIN exchanges e ON e.id = p.exchange_id
        WHERE s.symbol_name LIKE ?
        LIMIT ?
        """,
        (f"%{symbol}%", limit),
    ).fetchall()
    con.close()

    if not rows:
        typer.echo("No results found.")
        return

    if json_output:
        output = [
            {
                "symbol_name": r["symbol_name"],
                "symbol_kind": r["symbol_kind"],
                "file_path": r["file_path"],
                "signature": r["signature"],
                "line": r["line"],
                "exchange_id": r["exchange_id"],
                "exchange_core": r["exchange_core"],
                "specific_context": r["specific_context"],
                "user_content": r["user_content"],
                "agent_content": r["agent_content"],
            }
            for r in rows
        ]
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(rows, 1):
            typer.echo(f"\n[{i}] {r['symbol_kind']} {r['symbol_name']}")
            typer.echo(f"    {r['file_path']}:{r['line']}")
            typer.echo(f"    {r['signature']}")
            if r["exchange_core"]:
                typer.echo(f"    Core: {r['exchange_core']}")


# ---- status ----


@app.command()
def status(
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """インデックス状態（exchange 数・蒸留済み数・DB サイズ）を表示する"""
    from codeatrium.db import get_connection

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    total = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    distilled = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NOT NULL"
    ).fetchone()[0]
    palace_count = con.execute("SELECT COUNT(*) FROM palace_objects").fetchone()[0]
    symbol_count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    con.close()

    db_size_bytes = db.stat().st_size
    db_size_kb = db_size_bytes / 1024

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "db_path": str(db),
                    "exchanges": total,
                    "distilled": distilled,
                    "undistilled": total - distilled,
                    "palace_objects": palace_count,
                    "symbols": symbol_count,
                    "db_size_kb": round(db_size_kb, 1),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"DB: {db} ({db_size_kb:.1f} KB)")
        typer.echo(
            f"Exchanges : {total} total, {distilled} distilled, {total - distilled} pending"
        )
        typer.echo(f"Palace    : {palace_count}")
        typer.echo(f"Symbols   : {symbol_count}")


# ---- hook ----

hook_app = typer.Typer(help="Claude Code hook 管理")
app.add_typer(hook_app, name="hook")


def _loci_bin() -> str:
    """sys.executable と同じ venv の bin/loci のフルパスを返す（PATH 非依存）。"""
    import sys

    return str(Path(sys.executable).parent / "loci")


@hook_app.command("install")
def hook_install() -> None:
    """Claude Code の Stop / SessionStart フックに loci を登録する。

    Stop (async):      loci index    — 毎ターン・ノンブロッキング
    SessionStart:      loci distill  — CC起動・/clear・/resume・compact 時
                       claude --print サブセッションは SessionStart を発火しないためループなし
    """
    settings_path = Path.home() / ".claude" / "settings.json"

    if settings_path.exists():
        with settings_path.open() as f:
            settings: dict[str, Any] = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    loci = _loci_bin()
    index_cmd = f"{loci} index"
    distill_cmd = f"nohup {loci} distill > /dev/null 2>&1 &"
    server_cmd = f"nohup {loci} server start > /dev/null 2>&1 &"
    changed = False

    # --- Stop hook: loci index (async: true) ---
    stop_hooks: list[dict[str, Any]] = hooks.setdefault("Stop", [])
    stop_installed = False
    for entry in stop_hooks:
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "index" in h.get("command", ""):
                stop_installed = True
                if h.get("command") != index_cmd or not h.get("async"):
                    h["command"] = index_cmd
                    h["async"] = True
                    h.pop("nohup", None)
                    changed = True
    if not stop_installed:
        stop_hooks.append(
            {"hooks": [{"type": "command", "command": index_cmd, "async": True}]}
        )
        changed = True

    # --- SessionStart hook: loci server start + loci distill (nohup detach) ---
    # matcher: startup|clear|resume|compact — ユーザー起点のセッション境界のみ発火
    # server start: embedding サーバーをウォームアップ（アイドル10分で自動停止）
    # distill: claude --print サブセッションは SessionStart を発火しないためループなし
    session_start_hooks: list[dict[str, Any]] = hooks.setdefault("SessionStart", [])

    # server start エントリの確認・追加
    server_start_installed = False
    for entry in session_start_hooks:
        if entry.get("matcher") != "startup|clear|resume|compact":
            continue
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "server" in h.get("command", ""):
                server_start_installed = True
                if h.get("command") != server_cmd:
                    h["command"] = server_cmd
                    changed = True

    # distill エントリの確認・追加
    session_start_installed = False
    for entry in session_start_hooks:
        if entry.get("matcher") != "startup|clear|resume|compact":
            continue
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "distill" in h.get("command", ""):
                session_start_installed = True
                if h.get("command") != distill_cmd:
                    h["command"] = distill_cmd
                    changed = True

    if not server_start_installed or not session_start_installed:
        # 既存エントリを探して hooks を追加、なければ新規作成
        target_entry = next(
            (
                e
                for e in session_start_hooks
                if e.get("matcher") == "startup|clear|resume|compact"
            ),
            None,
        )
        if target_entry is None:
            target_entry = {"matcher": "startup|clear|resume|compact", "hooks": []}
            session_start_hooks.append(target_entry)
        hooks_list = cast(list[dict[str, Any]], target_entry["hooks"])
        if not server_start_installed:
            hooks_list.append({"type": "command", "command": server_cmd})
            changed = True
        if not session_start_installed:
            hooks_list.append({"type": "command", "command": distill_cmd})
            changed = True

    # 古い SessionEnd の loci distill エントリがあれば削除
    if "SessionEnd" in hooks:
        hooks["SessionEnd"] = [
            entry
            for entry in hooks["SessionEnd"]
            if not any(
                "loci" in h.get("command", "") and "distill" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        if not hooks["SessionEnd"]:
            del hooks["SessionEnd"]
        changed = True

    if not changed:
        typer.echo("Hooks already up to date.")
        return

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

    typer.echo(f"Hooks installed: {settings_path}")
    typer.echo(f"  Stop (async):       {index_cmd}")
    typer.echo(f"  SessionStart:       {server_cmd}")
    typer.echo(f"  SessionStart:       {distill_cmd}")
    typer.echo("  (matcher: startup|clear|resume|compact)")


# ---- server ----

server_app = typer.Typer(help="embedding サーバー管理")
app.add_typer(server_app, name="server")


def _sock_path(root: Path) -> Path:
    return _db_path(root).parent / "embedder.sock"


def _server_pid_path(root: Path) -> Path:
    return _db_path(root).parent / "embedder.pid"


@server_app.command("start")
def server_start() -> None:
    """embedding サーバーをバックグラウンドで起動する"""
    import subprocess

    root = _find_project_root()
    sock = _sock_path(root)

    if sock.exists():
        # ping して生死確認
        import socket as _socket

        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(str(sock))
                import json as _json

                s.sendall((_json.dumps({"type": "ping"}) + "\n").encode())
                resp = s.recv(256)
                if b"ok" in resp:
                    typer.echo("Server is already running.")
                    return
        except Exception:
            sock.unlink(missing_ok=True)

    pid_path = _server_pid_path(root)
    # venv の Python を使う（sys.executable はシステム Python の場合があるため）
    from codeatrium.embedder import _loci_python

    proc = subprocess.Popen(
        [_loci_python(), "-m", "codeatrium.embedder_server", str(sock)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid))

    # 起動待ち（最大 30 秒）
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

    root = _find_project_root()
    sock = _sock_path(root)

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

    _server_pid_path(root).unlink(missing_ok=True)


@server_app.command("status")
def server_status() -> None:
    """embedding サーバーの状態を確認する"""
    import json as _json
    import socket as _socket

    root = _find_project_root()
    sock = _sock_path(root)

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
                pid_path = _server_pid_path(root)
                pid = pid_path.read_text().strip() if pid_path.exists() else "unknown"
                typer.echo(f"Server: running (PID {pid})")
                typer.echo(f"Socket: {sock}")
                return
    except Exception:
        pass

    typer.echo("Server: socket exists but not responding")
    sock.unlink(missing_ok=True)


# ---- show ----


@app.command()
def show(
    ref: Annotated[str, typer.Argument(help="verbatim_ref (path:ply=N)")],
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """verbatim_ref から exchange の原文を取得する"""
    from codeatrium.db import get_connection

    # ref をパース: "path:ply=N"
    if ":ply=" not in ref:
        typer.echo("Invalid ref format. Expected: <path>:ply=<N>", err=True)
        raise typer.Exit(1)
    path_part, ply_part = ref.rsplit(":ply=", 1)
    try:
        ply = int(ply_part)
    except ValueError:
        typer.echo(f"Invalid ply value: {ply_part}", err=True)
        raise typer.Exit(1)

    root = _find_project_root()
    db = _db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    row = con.execute(
        """
        SELECT e.user_content, e.agent_content, e.ply_start, e.ply_end
        FROM exchanges e
        JOIN conversations c ON c.id = e.conversation_id
        WHERE c.source_path = ? AND e.ply_start = ?
        """,
        (path_part, ply),
    ).fetchone()
    con.close()

    if row is None:
        typer.echo("Exchange not found.")
        return

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "user_content": row["user_content"],
                    "agent_content": row["agent_content"],
                    "ply_start": row["ply_start"],
                    "ply_end": row["ply_end"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"[User] (ply {row['ply_start']}-{row['ply_end']})")
        typer.echo(row["user_content"])
        typer.echo("\n[Agent]")
        typer.echo(row["agent_content"])


# ---- dump ----


@app.command()
def dump(
    distilled: Annotated[
        bool, typer.Option("--distilled", help="蒸留済み palace objects を出力")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="最大件数")] = 1000,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """蒸留済み palace objects を新しい順に出力する（セッション開始時の in-context ロード用）"""
    from codeatrium.db import get_connection

    if not distilled:
        typer.echo("Use --distilled to dump palace objects.", err=True)
        raise typer.Exit(1)

    root = _find_project_root()
    db = _db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    rows = con.execute(
        """
        SELECT p.id, p.exchange_id, p.exchange_core, p.specific_context,
               e.distilled_at
        FROM palace_objects p
        JOIN exchanges e ON e.id = p.exchange_id
        ORDER BY e.distilled_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        typer.echo("No distilled objects found.")
        con.close()
        return

    palace_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(palace_ids))
    room_rows = con.execute(
        f"""
        SELECT palace_object_id, room_type, room_key, room_label
        FROM rooms
        WHERE palace_object_id IN ({placeholders})
        ORDER BY relevance DESC
        """,
        palace_ids,
    ).fetchall()
    con.close()

    rooms_map: dict[str, list[Any]] = {}
    for r in room_rows:
        rooms_map.setdefault(r["palace_object_id"], []).append(
            {
                "room_type": r["room_type"],
                "room_key": r["room_key"],
                "room_label": r["room_label"],
            }
        )

    if json_output:
        output = [
            {
                "exchange_core": r["exchange_core"],
                "specific_context": r["specific_context"],
                "rooms": rooms_map.get(r["id"], []),
                "date": (r["distilled_at"] or "")[:10],
            }
            for r in rows
        ]
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            date = (r["distilled_at"] or "")[:10]
            typer.echo(f"\n[{date}] {r['exchange_core']}")
            if r["specific_context"]:
                typer.echo(f"  {r['specific_context']}")
            for rm in rooms_map.get(r["id"], [])[:2]:
                typer.echo(f"  #{rm['room_key']}")
