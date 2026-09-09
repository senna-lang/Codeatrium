"""loci recall — セッション開始ウォームアップ複合コマンド。

`--file` と `--branch` は独立した AND フィルタ（片方だけでも両方でも可）。
コードアンカーの context 検索と `search_combined` をマージ・重複排除し、
context と同じ要約形（exchange_core / specific_context / verbatim_ref）で返す。
ランキングは recency 減衰（search.py）を既定でかける。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer


def recall(
    file_path: Annotated[
        str | None,
        typer.Option("--file", help="ファイルパスで絞り込む（プロジェクト相対）"),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="ブランチ名で絞り込む（部分一致）"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
    full: Annotated[
        bool,
        typer.Option("--full", help="全文（user_content / agent_content）を含める"),
    ] = False,
    recency_half_life_days: Annotated[
        float,
        typer.Option(
            "--recency-half-life",
            help="RRF/confidence に掛ける指数減衰の半減期（日）。0 で無効",
        ),
    ] = 14.0,
) -> None:
    """context + search を束ね、新しい決定を優先して返す。"""
    if file_path is None and branch is None:
        typer.echo("Error: --file or --branch is required.", err=True)
        raise typer.Exit(1)

    from codeatrium.cli.search_cmd import (
        _print_context_hits,
        _resolve_target_file_path,
    )
    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    resolved_file: str | None = None
    if file_path is not None:
        resolved_file = _resolve_target_file_path(file_path, str(root))
        if resolved_file is None:
            typer.echo(
                f"Error: {file_path} is outside the project.",
                err=True,
            )
            raise typer.Exit(1)

    con = get_connection(db)
    try:
        alias_paths: tuple[str, ...] = ()
        if resolved_file is not None:
            from codeatrium.file_renames import resolve_aliases

            alias_paths = tuple(resolve_aliases(con, str(root), resolved_file))
        context_hits = _context_hits(con, resolved_file, branch, limit * 2, alias_paths)
        query_text = _recall_query_text(con, resolved_file, branch)
    finally:
        con.close()

    search_hits = _semantic_hits(
        db, query_text, limit * 2, branch, recency_half_life_days
    )
    merged = _merge_hits(context_hits, search_hits)
    if recency_half_life_days > 0 and merged:
        merged = _rank_hits_by_recency(db, merged, recency_half_life_days)
    merged = merged[:limit]

    if not merged:
        typer.echo("No results found.")
        return

    _print_context_hits(merged, json_output, full)


def _context_hits(
    con, file_path: str | None, branch: str | None, limit: int, alias_paths
):
    from codeatrium.context_lookup import ContextHit, resolve_u2

    if file_path is not None:
        return resolve_u2(con, file_path, limit, alias_paths, branch=branch)

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
        ORDER BY c.started_at DESC, e.ply_start DESC
        LIMIT ?
        """,
        (f"%{branch}%", limit),
    ).fetchall()
    return [
        ContextHit(
            match_kind="branch",
            confidence=1.0,
            exchange_id=r["exchange_id"],
            file_path="",
            symbol_name=None,
            exchange_core=r["exchange_core"],
            specific_context=r["specific_context"],
            verbatim_ref=f"{r['source_path']}:ply={r['ply_start']}",
            git_branch=r["git_branch"],
            user_content=r["user_content"],
            agent_content=r["agent_content"],
        )
        for r in rows
    ]


def _recall_query_text(con, file_path: str | None, branch: str | None) -> str:
    from codeatrium.cli.search_cmd import _semantic_query_text

    parts: list[str] = []
    clauses: list[str] = []
    params: list[object] = []
    if file_path is not None:
        clauses.append("ce.file_path = ?")
        params.append(file_path)
    if branch is not None:
        clauses.append("e.git_branch LIKE ?")
        params.append(f"%{branch}%")
    if clauses:
        where = " AND ".join(clauses)
        rows = con.execute(
            f"""
            SELECT s.symbol_name
            FROM code_edges ce
            JOIN exchanges e ON e.id = ce.exchange_id
            JOIN code_symbols s ON s.id = ce.symbol_id
            WHERE {where}
            ORDER BY ce.ts DESC
            LIMIT 8
            """,
            params,
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            name = row["symbol_name"]
            if name and name not in seen:
                seen.add(name)
                parts.append(name)
            if len(parts) >= 3:
                break
    if file_path is not None:
        parts.append(_semantic_query_text(file_path, None))
    elif not parts and branch:
        parts.append(branch)
    return " ".join(parts)


def _semantic_hits(
    db,
    query_text: str,
    limit: int,
    branch: str | None,
    recency_half_life_days: float,
):
    from codeatrium.context_lookup import ContextHit
    from codeatrium.embedder import Embedder
    from codeatrium.search import search_combined

    if not query_text:
        return []

    embedder = Embedder()
    query_vec = embedder.embed(query_text)
    half_life = recency_half_life_days if recency_half_life_days > 0 else None
    results = search_combined(
        db,
        query_text,
        query_vec,
        limit=limit,
        branch=branch,
        recency_half_life_days=half_life,
    )
    return [
        ContextHit(
            match_kind="semantic",
            confidence=0.10,
            exchange_id=r.exchange_id,
            file_path="",
            symbol_name=None,
            exchange_core=r.exchange_core,
            specific_context=r.specific_context,
            verbatim_ref=r.verbatim_ref,
            git_branch=r.git_branch,
            user_content=r.user_content,
            agent_content=r.agent_content,
        )
        for r in results
    ]


def _merge_hits(context_hits, search_hits):
    seen: set[str] = set()
    out = []
    for hit in (*context_hits, *search_hits):
        if hit.exchange_id in seen:
            continue
        seen.add(hit.exchange_id)
        out.append(hit)
    return out


def _rank_hits_by_recency(db, hits, half_life_days: float):
    from codeatrium.db import get_connection
    from codeatrium.search import (
        exchange_timestamps,
        recency_weight,
    )

    con = get_connection(db)
    try:
        timestamps = exchange_timestamps(con, [h.exchange_id for h in hits])
    finally:
        con.close()

    now = datetime.now(UTC)

    def sort_key(hit) -> float:
        ts = timestamps.get(hit.exchange_id)
        if ts is None:
            return hit.confidence
        age = max(0.0, (now - ts).total_seconds() / 86400.0)
        return hit.confidence * recency_weight(age, half_life_days)

    return sorted(hits, key=sort_key, reverse=True)
