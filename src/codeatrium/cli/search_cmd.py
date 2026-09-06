"""
loci context（主機能・コードから会話を思い出す） / loci search（副次機能・言葉で探す）
コマンド（design §6.1）。

loci context は3つの引き方を持つ:
  U1: `<file>:<symbol>`（design §6.0）— ファイル内の関数・コンポーネントで引く。主な使い方
  U2: `<file>`（design §6.0）— ファイルそのもので引く
  行番号: `<file>:<line>`（design §6.1）— 含む関数へ変換して U1 として扱う（IDE選択範囲用）
どちらも見つからなければ、symbol/file/directory の順に確信度を下げて段階的に探し
（design §6.2）、最後は言葉によるセマンティック検索にフォールバックする。
既存の `--symbol`/`--branch` フラグは互換のため残す（位置引数が無いときのみ使われる）。
"""

from __future__ import annotations

import json
import posixpath
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
                "exchange_id": r.exchange_id,
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
    target: Annotated[
        str | None,
        typer.Argument(
            help='<file>[:<symbol-or-line>]  例: "src/foo.py:greet"（U1、主な使い方）'
            ' / "src/foo.py"（U2） / "src/foo.py:142"（行番号、IDE選択範囲用）'
        ),
    ] = None,
    symbol: Annotated[
        str | None,
        typer.Option(
            "--symbol", "-s", help="シンボル名（部分一致・非推奨）。ファイル指定付きの位置引数を推奨"
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="返す件数")] = 5,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
    full: Annotated[bool, typer.Option("--full", help="全文（user_content / agent_content）を含める")] = False,
    branch: Annotated[str | None, typer.Option("--branch", "-b", help="ブランチ名で絞り込む（部分一致）")] = None,
) -> None:
    """コードから会話を思い出す（design §6.1 主機能）。

    位置引数（U1/U2）があればそちらを使う。無ければ --symbol/--branch にフォールバックする。
    """
    if target is not None:
        if symbol is not None or branch is not None:
            typer.echo(
                "Warning: positional <file>[:<symbol>] takes precedence over --symbol/--branch.",
                err=True,
            )
        _context_u1_u2(target, limit, json_output, full)
        return

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


# ---- U1/U2（design §6.1・§6.2） ----


def _resolve_target_file_path(file_path: str, project_root: str) -> str | None:
    """target のファイル部分を code_edges/code_symbols の規約（プロジェクトルート
    相対パス）へ揃える。絶対パス（agent が Read/Edit から得るのは普通これ）は
    normalize_repo_path で変換し、プロジェクト外なら None を返す。相対パスは
    既にその規約どおりであるとみなしそのまま使う（design の例が一貫してこの形）。
    """
    if file_path.startswith("/"):
        from codeatrium.code_touches import normalize_repo_path

        return normalize_repo_path(file_path, project_root)
    return file_path


def _semantic_query_text(file_path: str, symbol_name: str | None) -> str:
    """semantic 段（design §6.2 最終段）のクエリ文字列。
    U1: シンボル名とモジュール名。U2: モジュール名とファイルパス。

    モジュール名は拡張子を除いたファイル名（例: "config.py" -> "config"）。
    BM25 は空白区切りでトークン化するため（`_fts5_query`）、フルパスだけを渡すと
    1つの完全一致フレーズになってしまい、自然文の会話とはほぼマッチしない。
    実際に語として現れやすいモジュール名を主なトークンにする。
    """
    module_name = posixpath.splitext(posixpath.basename(file_path))[0]
    if symbol_name:
        return f"{symbol_name} {module_name}"
    return f"{module_name} {file_path}"


def _semantic_fallback_hits(
    db, file_path: str, symbol_name: str | None, limit: int
):
    """symbol/file/directory 段が全て空だったときの最終フォールバック（design §6.2）。
    embedding を使うため、このモジュール（CLI層）でのみ組み立てる
    （context_lookup.py は embedding に依存させない、design の意図的な分離）"""
    from codeatrium.context_lookup import ContextHit
    from codeatrium.embedder import Embedder
    from codeatrium.search import search_combined

    query_text = _semantic_query_text(file_path, symbol_name)
    embedder = Embedder()
    query_vec = embedder.embed(query_text)
    results = search_combined(db, query_text, query_vec, limit=limit)
    return [
        ContextHit(
            match_kind="semantic",
            confidence=0.10,
            exchange_id=r.exchange_id,
            file_path=file_path,
            symbol_name=symbol_name,
            exchange_core=r.exchange_core,
            specific_context=r.specific_context,
            verbatim_ref=r.verbatim_ref,
            git_branch=r.git_branch,
            user_content=r.user_content,
            agent_content=r.agent_content,
        )
        for r in results
    ]


def _print_context_hits(hits, json_output: bool, full: bool) -> None:
    if json_output:
        output = []
        for h in hits:
            item = {
                "match_kind": h.match_kind,
                "confidence": h.confidence,
                "symbol_name": h.symbol_name,
                "file_path": h.file_path,
                "exchange_id": h.exchange_id,
                "exchange_core": h.exchange_core,
                "specific_context": h.specific_context,
                "verbatim_ref": h.verbatim_ref,
                "git_branch": h.git_branch,
                "context": [
                    {
                        "relation": s.relation,
                        "exchange_id": s.exchange_id,
                        "ply": s.ply,
                        "exchange_core": s.exchange_core,
                        "specific_context": s.specific_context,
                        "verbatim_ref": s.verbatim_ref,
                        **(
                            {"user_content": s.user_content, "agent_content": s.agent_content}
                            if full
                            else {}
                        ),
                    }
                    for s in h.context
                ],
            }
            if full:
                item["user_content"] = h.user_content
                item["agent_content"] = h.agent_content
            output.append(item)
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, h in enumerate(hits, 1):
            label = h.symbol_name or h.file_path
            typer.echo(f"\n[{i}] {h.match_kind} (confidence={h.confidence:.2f}) {label}")
            typer.echo(f"    {h.file_path}")
            if h.exchange_core:
                typer.echo(f"    Core: {h.exchange_core}")
            if h.verbatim_ref:
                typer.echo(f"    {h.verbatim_ref}")
            if h.context:
                labels = {"ply_adjacent": "同一会話の前後", "parent_session": "親会話（同一ファイル編集）"}
                for s in h.context:
                    typer.echo(f"    + [{labels.get(s.relation, s.relation)}] {s.exchange_core or s.user_content[:80]}")


def _context_u1_u2(target: str, limit: int, json_output: bool, full: bool) -> None:
    from codeatrium.context_lookup import (
        parse_context_target,
        pick_enclosing_symbol_name,
        resolve_u1,
        resolve_u2,
    )
    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    parsed = parse_context_target(target)
    file_path = _resolve_target_file_path(parsed.file_path, str(root))
    if file_path is None:
        typer.echo(f"Error: {parsed.file_path} is outside the project.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)

    from codeatrium.file_renames import resolve_aliases

    alias_paths = tuple(resolve_aliases(con, str(root), file_path))

    symbol_name = parsed.symbol_name
    if parsed.line is not None:
        sym_rows = con.execute(
            "SELECT symbol_name, line, end_line FROM code_symbols WHERE file_path = ?",
            (file_path,),
        ).fetchall()
        symbols = [(r["symbol_name"], r["line"], r["end_line"]) for r in sym_rows]
        symbol_name = pick_enclosing_symbol_name(parsed.line, symbols)

    hits = (
        resolve_u1(con, file_path, symbol_name, limit, alias_paths)
        if symbol_name is not None
        else resolve_u2(con, file_path, limit, alias_paths)
    )
    con.close()

    if not hits:
        hits = _semantic_fallback_hits(db, file_path, symbol_name, limit)

    if not hits:
        typer.echo("No results found.")
        return

    _print_context_hits(hits, json_output, full)
