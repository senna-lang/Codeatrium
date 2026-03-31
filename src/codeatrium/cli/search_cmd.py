"""loci search / loci context コマンド"""

from __future__ import annotations

import json
from typing import Annotated

import typer


def search(
    query: Annotated[str, typer.Argument(help="検索クエリ")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
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


def context(
    symbol: Annotated[
        str, typer.Option("--symbol", "-s", help="シンボル名（部分一致）")
    ],
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """シンボル名から関連する過去会話を逆引きする"""
    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

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
