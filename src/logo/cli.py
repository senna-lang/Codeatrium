"""
logo CLI エントリポイント

コマンド:
  logo init    - .logosyncs/memory.db を初期化
  logo index   - ~/.claude/projects/ の .jsonl を処理
  logo search  - HNSW セマンティック検索
"""

from __future__ import annotations

import json
import struct
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
    """未処理の .jsonl を処理して exchanges テーブルと vec_exchanges に登録する"""
    from logo.db import get_connection, init_db
    from logo.embedder import Embedder
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

    embedder = Embedder()
    total_exchanges = 0

    for jsonl in new_files:
        count = index_file(jsonl, db)
        if count == 0:
            continue
        if verbose:
            typer.echo(f"  {jsonl.name}: {count} exchanges")

        # Phase 1: verbatim embedding を vec_exchanges に登録
        con = get_connection(db)
        unvectorized = con.execute(
            """
            SELECT e.id, e.user_content, e.agent_content
            FROM exchanges e
            LEFT JOIN vec_exchanges v ON v.exchange_id = e.id
            WHERE v.exchange_id IS NULL
            """
        ).fetchall()

        for row in unvectorized:
            text = f"{row['user_content']}\n{row['agent_content']}"
            vec = embedder.embed_passage(text)
            blob = struct.pack(f"{len(vec)}f", *vec.tolist())
            con.execute(
                "INSERT OR IGNORE INTO vec_exchanges (exchange_id, embedding) VALUES (?, ?)",
                (row["id"], blob),
            )

        con.commit()
        con.close()
        total_exchanges += count

    typer.echo(f"Indexed {len(new_files)} file(s), {total_exchanges} exchange(s).")


# ---- search ----


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """セマンティック検索でクエリに近い過去会話を返す"""
    from logo.embedder import Embedder
    from logo.search import search_hnsw

    root = _find_project_root()
    db = _db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `logo init` first.", err=True)
        raise typer.Exit(1)

    embedder = Embedder()
    query_vec = embedder.embed(query)
    results = search_hnsw(db, query_vec, limit=limit)

    if not results:
        typer.echo("No results found.")
        return

    if json_output:
        output = [
            {
                "exchange_id": r.exchange_id,
                "user_content": r.user_content,
                "agent_content": r.agent_content,
                "distance": r.distance,
            }
            for r in results
        ]
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(results, 1):
            typer.echo(f"\n[{i}] distance={r.distance:.4f}")
            typer.echo(f"    Q: {r.user_content[:100]}")
            typer.echo(f"    A: {r.agent_content[:100]}")
