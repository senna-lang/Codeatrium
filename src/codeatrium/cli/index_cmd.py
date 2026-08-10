"""loci index コマンド"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

_HARNESS_CHOICES = {"claude", "codex", "opencode"}


def index(
    path: Annotated[
        Path | None,
        typer.Option(
            help="インデックス対象パス（claude/codex はディレクトリ、opencode は DB ファイル）"
        ),
    ] = None,
    harness: Annotated[
        str, typer.Option("--harness", help="ログ形式（claude / codex / opencode）")
    ] = "claude",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """指定ハーネスの未処理ログを exchange とコード編集記録へ取り込む。"""
    from codeatrium.config import load_config
    from codeatrium.db import init_db
    from codeatrium.indexer import index_file, index_opencode_db
    from codeatrium.paths import (
        db_path,
        find_project_root,
        resolve_claude_projects_path,
        resolve_codex_sessions_path,
        resolve_opencode_db_path,
    )

    root = find_project_root()
    db = db_path(root)

    if not db.exists() and not (root / ".codeatrium").exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)
    if harness not in _HARNESS_CHOICES:
        choices = ", ".join(sorted(_HARNESS_CHOICES))
        typer.echo(f"Unsupported harness. Choose one of: {choices}.", err=True)
        raise typer.Exit(1)

    init_db(db)
    from codeatrium.db import check_drift

    drifts = check_drift(db)
    for key, recorded, current in drifts:
        typer.echo(
            f"[warn] {key} changed ({recorded} -> {current}). Re-index recommended.",
            err=True,
        )
    cfg = load_config(root)

    if harness == "opencode":
        opencode_db = path or resolve_opencode_db_path()
        if opencode_db is None or not opencode_db.exists():
            typer.echo(
                "OpenCode session DB not found. Use --path to specify.", err=True
            )
            raise typer.Exit(1)

        total_exchanges = index_opencode_db(
            opencode_db, db, min_chars=cfg.index_min_chars, project_root=root
        )
        if total_exchanges == 0:
            typer.echo("Nothing new to index.")
            return
        typer.echo(f"Indexed 1 file(s), {total_exchanges} exchange(s).")
        return

    if path is not None:
        target_dir = path
    elif harness == "claude":
        target_dir = resolve_claude_projects_path(root)
    else:
        target_dir = resolve_codex_sessions_path()
    if target_dir is None:
        typer.echo(
            f"{harness.capitalize()} sessions dir not found. Use --path to specify.",
            err=True,
        )
        raise typer.Exit(1)

    pattern = "rollout-*.jsonl" if harness == "codex" else "*.jsonl"
    jsonl_files = list(target_dir.rglob(pattern))
    if not jsonl_files:
        typer.echo("No session files found.")
        return

    total_exchanges = 0
    files_with_new = 0
    for jsonl in jsonl_files:
        count = index_file(
            jsonl,
            db,
            min_chars=cfg.index_min_chars,
            project_root=root,
            harness=harness,
        )
        if count == 0:
            continue
        files_with_new += 1
        if verbose:
            typer.echo(f"  {jsonl.name}: {count} exchanges")
        total_exchanges += count

    if total_exchanges == 0:
        typer.echo("Nothing new to index.")
        return

    typer.echo(f"Indexed {files_with_new} file(s), {total_exchanges} exchange(s).")
