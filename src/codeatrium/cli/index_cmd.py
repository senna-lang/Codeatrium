"""loci index コマンド"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

_HARNESS_CHOICES = ("claude", "codex", "opencode", "omp-pi", "grok")

# ハーネスごとのログファイル名パターン。同じディレクトリに別形式のファイルが
# 同居することがあるため（grok の prompt_history.jsonl / events.jsonl）、
# 素朴な "*.jsonl" では拾いすぎる。
_LOG_PATTERNS = {"codex": "rollout-*.jsonl", "grok": "updates.jsonl"}

def _codex_belongs_to_project(rollout: Path, project_root: Path) -> bool:
    """Accept only rollout logs whose recorded cwd is inside project_root."""
    root = project_root.resolve()
    try:
        with rollout.open() as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") not in {"session_meta", "turn_context"}:
                    continue
                payload = entry.get("payload")
                cwd = payload.get("cwd") if isinstance(payload, dict) else None
                if not isinstance(cwd, str) or not cwd:
                    continue
                try:
                    Path(cwd).resolve().relative_to(root)
                except ValueError:
                    return False
                return True
    except OSError:
        return False
    return False

def _purge_foreign_codex_exchanges(db: Path, project_root: Path) -> int:
    """Remove previously indexed Codex rollouts outside the active project."""
    from codeatrium.db import get_connection

    con = get_connection(db)
    try:
        rows = con.execute(
            "SELECT id, session_ref FROM exchanges WHERE harness = 'codex'"
        ).fetchall()
        scope_by_rollout: dict[str, bool] = {}
        foreign_ids: list[str] = []
        for exchange_id, session_ref in rows:
            rollout = session_ref.partition("#")[0]
            in_scope = scope_by_rollout.get(rollout)
            if in_scope is None:
                rollout_path = Path(rollout)
                in_scope = (
                    True
                    if not rollout_path.exists()
                    else _codex_belongs_to_project(rollout_path, project_root)
                )
                scope_by_rollout[rollout] = in_scope
            if not in_scope:
                foreign_ids.append(exchange_id)
        if not foreign_ids:
            return 0
        placeholders = ",".join("?" for _ in foreign_ids)
        palace_ids = (
            "SELECT id FROM palace_objects "
            f"WHERE exchange_id IN ({placeholders})"
        )
        con.execute(
            f"DELETE FROM rooms WHERE palace_object_id IN ({palace_ids})",
            foreign_ids,
        )
        con.execute(
            f"DELETE FROM symbols WHERE palace_object_id IN ({palace_ids})",
            foreign_ids,
        )
        con.execute(
            f"DELETE FROM vec_palace WHERE palace_id IN ({palace_ids})",
            foreign_ids,
        )
        for table in ("exchange_files", "code_touches", "code_edges", "vec_exchanges"):
            con.execute(
                f"DELETE FROM {table} WHERE exchange_id IN ({placeholders})",
                foreign_ids,
            )
        con.execute(
            f"DELETE FROM palace_objects WHERE exchange_id IN ({placeholders})",
            foreign_ids,
        )
        con.execute(
            f"DELETE FROM exchanges WHERE id IN ({placeholders})", foreign_ids
        )
        con.execute(
            """
            DELETE FROM sessions
            WHERE harness = 'codex'
              AND id NOT IN (SELECT DISTINCT session_id FROM exchanges)
            """
        )
        con.commit()
        return len(foreign_ids)
    finally:
        con.close()


def index(
    path: Annotated[
        Path | None,
        typer.Option(
            help="インデックス対象パス（opencode は DB ファイル、それ以外はディレクトリ）"
        ),
    ] = None,
    harness: Annotated[
        str,
        typer.Option(
            "--harness",
            help="ログ形式（all / claude / codex / opencode / omp-pi / grok）",
        ),
    ] = "all",
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
        resolve_grok_sessions_path,
        resolve_omp_pi_sessions_path,
        resolve_opencode_db_path,
    )

    root = find_project_root()
    db = db_path(root)

    if not db.exists() and not (root / ".codeatrium").exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)
    if harness == "all":
        from codeatrium.adapters.harness.registry import detected_jsonl_sources

        source_ids = [
            source.id for source in detected_jsonl_sources() if source.detect(root)
        ]
        if resolve_opencode_db_path() is not None:
            source_ids.append("opencode")
        if not source_ids:
            typer.echo("No detected harness sessions.")
            return
        for source_id in source_ids:
            index(path=None, harness=source_id, verbose=verbose)
        return
    if harness not in _HARNESS_CHOICES:
        choices = ", ".join(("all", *_HARNESS_CHOICES))
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
    if harness == "codex":
        _purge_foreign_codex_exchanges(db, root)

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
    elif harness == "omp-pi":
        target_dir = resolve_omp_pi_sessions_path(root)
    elif harness == "grok":
        target_dir = resolve_grok_sessions_path(root)
    else:
        target_dir = resolve_codex_sessions_path()
    if target_dir is None:
        typer.echo(
            f"{harness.capitalize()} sessions dir not found. Use --path to specify.",
            err=True,
        )
        raise typer.Exit(1)

    jsonl_files = list(target_dir.rglob(_LOG_PATTERNS.get(harness, "*.jsonl")))
    if harness == "codex":
        jsonl_files = [
            rollout
            for rollout in jsonl_files
            if _codex_belongs_to_project(rollout, root)
        ]
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
