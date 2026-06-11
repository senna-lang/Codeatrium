"""loci search / loci context コマンド"""

from __future__ import annotations

import json
from typing import Annotated

import typer


def search(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
    branch: Annotated[str | None, typer.Option("--branch", "-b", help="ブランチ名で絞り込む（部分一致）")] = None,
) -> None:
    """BM25(V) + HNSW(D) RRF でクエリに近い過去会話を返す"""
    from codeatrium.embedder import Embedder
    from codeatrium.paths import db_path, find_project_root
    from codeatrium.search import search_combined

    root = find_project_root()
    db = db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    from codeatrium.db import check_drift
    drifts = check_drift(db)
    for key, recorded, current in drifts:
        typer.echo(f"[warn] {key} changed ({recorded} -> {current}). Re-index recommended.", err=True)

    embedder = Embedder()
    query_vec = embedder.embed(query)
    results = search_combined(db, query, query_vec, limit=limit, branch=branch)

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
                "git_branch": r.git_branch,
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


def context(
    symbol: Annotated[str | None, typer.Option("--symbol", "-s", help="シンボル名（部分一致）")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
    full: Annotated[bool, typer.Option("--full", help="全文（user_content / agent_content）を含める")] = False,
    branch: Annotated[str | None, typer.Option("--branch", "-b", help="ブランチ名で絞り込む（部分一致）")] = None,
) -> None:
    """シンボル名から関連する過去会話を逆引きする"""
    if symbol is None and branch is None:
        typer.echo("Error: --symbol or --branch is required.", err=True)
        raise typer.Exit(1)

    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)

    if symbol is not None and branch is not None:
        # Both symbol and branch specified
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
                p.specific_context,
                c.source_path,
                e.ply_start,
                e.git_branch
            FROM symbols s
            JOIN palace_objects p ON p.id = s.palace_object_id
            JOIN exchanges e ON e.id = p.exchange_id
            JOIN conversations c ON c.id = e.conversation_id
            WHERE s.symbol_name LIKE ? AND e.git_branch LIKE ?
            LIMIT ?
            """,
            (f"%{symbol}%", f"%{branch}%", limit),
        ).fetchall()
    elif symbol is not None:
        # Symbol only (existing behavior with git_branch added)
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
                p.specific_context,
                c.source_path,
                e.ply_start,
                e.git_branch
            FROM symbols s
            JOIN palace_objects p ON p.id = s.palace_object_id
            JOIN exchanges e ON e.id = p.exchange_id
            JOIN conversations c ON c.id = e.conversation_id
            WHERE s.symbol_name LIKE ?
            LIMIT ?
            """,
            (f"%{symbol}%", limit),
        ).fetchall()
    else:
        # Branch only (LEFT JOIN to include undistilled exchanges)
        rows = con.execute(
            """
            SELECT
                e.id AS exchange_id,
                e.git_branch,
                e.user_content,
                e.agent_content,
                p.exchange_core,
                p.specific_context,
                c.source_path,
                e.ply_start
            FROM exchanges e
            JOIN conversations c ON c.id = e.conversation_id
            LEFT JOIN palace_objects p ON p.exchange_id = e.id
            WHERE e.git_branch LIKE ?
            ORDER BY c.started_at, e.ply_start
            LIMIT ?
            """,
            (f"%{branch}%", limit),
        ).fetchall()
    con.close()

    if not rows:
        typer.echo("No results found.")
        return

    if json_output:
        output = []
        for r in rows:
            if symbol is not None:
                # Symbol mode (symbol only or both)
                base = {
                    "symbol_name": r["symbol_name"],
                    "symbol_kind": r["symbol_kind"],
                    "file_path": r["file_path"],
                    "signature": r["signature"],
                    "line": r["line"],
                    "exchange_id": r["exchange_id"],
                    "exchange_core": r["exchange_core"],
                    "specific_context": r["specific_context"],
                    "verbatim_ref": f"{r['source_path']}:ply={r['ply_start']}",
                    "git_branch": r["git_branch"] if "git_branch" in r.keys() else None,
                }
                if full:
                    base["user_content"] = r["user_content"]
                    base["agent_content"] = r["agent_content"]
            else:
                # Branch-only mode
                base = {
                    "exchange_id": r["exchange_id"],
                    "git_branch": r["git_branch"],
                    "exchange_core": r["exchange_core"],
                    "specific_context": r["specific_context"],
                    "verbatim_ref": f"{r['source_path']}:ply={r['ply_start']}",
                }
                if full:
                    base["user_content"] = r["user_content"]
                    base["agent_content"] = r["agent_content"]
            output.append(base)
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(rows, 1):
            if symbol is not None:
                # Symbol mode display
                typer.echo(f"\n[{i}] {r['symbol_kind']} {r['symbol_name']}")
                typer.echo(f"    {r['file_path']}:{r['line']}")
                typer.echo(f"    {r['signature']}")
                if r["exchange_core"]:
                    typer.echo(f"    Core: {r['exchange_core']}")
                typer.echo(f"    {r['source_path']}:ply={r['ply_start']}")
            else:
                # Branch-only mode display
                typer.echo(f"\n[{i}] exchange_id={r['exchange_id']} git_branch={r['git_branch']}")
                if r["exchange_core"]:
                    typer.echo(f"    Core: {r['exchange_core']}")
                typer.echo(f"    {r['source_path']}:ply={r['ply_start']}")
