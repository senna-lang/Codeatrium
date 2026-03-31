"""loci index コマンド"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def index(
    path: Annotated[
        Path | None, typer.Option(help="インデックス対象ディレクトリ")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """未処理の .jsonl を処理して exchanges テーブルに登録する（FTS5 自動同期）"""
    from codeatrium.db import get_connection, init_db
    from codeatrium.indexer import index_file
    from codeatrium.paths import (
        db_path,
        find_project_root,
        resolve_claude_projects_path,
    )

    root = find_project_root()
    db = db_path(root)
    init_db(db)

    target_dir = path or resolve_claude_projects_path(root)
    if target_dir is None:
        typer.echo("Claude projects dir not found. Use --path to specify.", err=True)
        raise typer.Exit(1)

    jsonl_files = list(target_dir.rglob("*.jsonl"))
    if not jsonl_files:
        typer.echo("No .jsonl files found.")
        return

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
