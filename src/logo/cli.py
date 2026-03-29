"""
logo CLI エントリポイント

コマンド:
  logo init         - .logosyncs/memory.db を初期化
  logo index        - ~/.claude/projects/ の .jsonl を処理
  logo distill      - 未蒸留 exchange を claude -p で palace object に変換
  logo search       - BM25(V) + HNSW(D) RRF セマンティック検索
  logo context      - シンボル名から過去会話を逆引き
  logo status       - インデックス状態を表示
  logo hook install - Claude Code Stop hook を ~/.claude/settings.json に登録
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="CLI-first memory layer for AI coding agents")

# デフォルトの Claude Code セッションログ格納先
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
LOGOSYNCS_DIR = ".logosyncs"
DB_NAME = "memory.db"


def _find_project_root() -> Path:
    """.logosyncs/ ディレクトリを探してプロジェクトルートを返す。なければ cwd"""
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / LOGOSYNCS_DIR).exists():
            return p
    return cwd


def _db_path(project_root: Path) -> Path:
    return project_root / LOGOSYNCS_DIR / DB_NAME


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
    """プロジェクトルートに .logosyncs/memory.db を初期化する"""
    from logo.db import init_db

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
    from logo.db import get_connection, init_db
    from logo.indexer import index_file

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
def distill() -> None:
    """未蒸留の exchange を claude -p で蒸留して palace_objects を生成する"""
    from logo.distiller import distill_all

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `logo init` first.", err=True)
        raise typer.Exit(1)

    count = distill_all(db)
    typer.echo(f"Distilled {count} exchange(s).")


# ---- search ----


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """BM25(V) + HNSW(D) CombMNZ でクエリに近い過去会話を返す"""
    from logo.embedder import Embedder
    from logo.search import search_combined

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `logo init` first.", err=True)
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
                "exchange_id": r.exchange_id,
                "user_content": r.user_content,
                "agent_content": r.agent_content,
                "score": r.score,
                "exchange_core": r.exchange_core,
                "specific_context": r.specific_context,
            }
            for r in results
        ]
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(results, 1):
            typer.echo(f"\n[{i}] score={r.score:.4f}")
            if r.exchange_core:
                typer.echo(f"    Core: {r.exchange_core}")
            typer.echo(f"    Q: {r.user_content[:100]}")
            typer.echo(f"    A: {r.agent_content[:100]}")


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
    from logo.db import get_connection

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `logo init` first.", err=True)
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
    from logo.db import get_connection

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `logo init` first.", err=True)
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

_HOOK_COMMAND = "logo index && nohup logo distill > /dev/null 2>&1 &"

hook_app = typer.Typer(help="Claude Code Stop hook 管理")
app.add_typer(hook_app, name="hook")


@hook_app.command("install")
def hook_install() -> None:
    """Claude Code の Stop hook に logo index && logo distill を登録する"""
    settings_path = Path.home() / ".claude" / "settings.json"

    if settings_path.exists():
        with settings_path.open() as f:
            settings: dict = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    stop_hooks: list = hooks.setdefault("Stop", [])

    # 既存エントリ確認
    for entry in stop_hooks:
        for h in entry.get("hooks", []):
            if h.get("command") == _HOOK_COMMAND:
                typer.echo("Hook already installed.")
                return

    stop_hooks.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": _HOOK_COMMAND,
                }
            ]
        }
    )

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

    typer.echo(f"Hook installed: {settings_path}")
    typer.echo(f"  Command: {_HOOK_COMMAND}")
