"""loci CLI エントリポイント — サブコマンド登録のみ"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from codeatrium.cli.distill_cmd import distill
from codeatrium.cli.hook_cmd import hook_app
from codeatrium.cli.index_cmd import index
from codeatrium.cli.prime_cmd import prime
from codeatrium.cli.search_cmd import context, search
from codeatrium.cli.server_cmd import server_app
from codeatrium.cli.show_cmd import dump, show
from codeatrium.cli.status_cmd import status

app = typer.Typer(help="CLI-first memory layer for AI coding agents")

DEFAULT_DISTILL_RECENT = 50


@app.command()
def init(
    skip_existing: Annotated[
        bool,
        typer.Option("--skip-existing", help="既存 exchange の蒸留をスキップする"),
    ] = False,
    distill_limit: Annotated[
        int | None,
        typer.Option("--distill-limit", help="既存 exchange のうち直近 N 件のみ蒸留対象にする"),
    ] = None,
    min_chars: Annotated[
        int | None,
        typer.Option("--min-chars", help="既存 exchange の最小文字数フィルタ（省略時は対話で選択）"),
    ] = None,
) -> None:
    """プロジェクトルートに .codeatrium/memory.db を初期化する"""
    from codeatrium.db import get_connection, init_db
    from codeatrium.indexer import index_file, parse_exchanges
    from codeatrium.paths import (
        db_path,
        find_project_root,
        resolve_claude_projects_path,
    )

    root = find_project_root()
    db = db_path(root)

    if db.exists():
        typer.echo(f"Already initialized: {db}")
        return

    # 既存セッションの検出
    target_dir = resolve_claude_projects_path(root)
    jsonl_files = list(target_dir.rglob("*.jsonl")) if target_dir else []

    # --- 対話フェーズ（DB 作成前にすべての質問を完了する） ---
    resolved_min_chars = 50
    skip_count = 0
    skip_strategy = "recent"
    total_exchanges = 0
    run_distill_now = False

    if jsonl_files:
        resolved_min_chars = _resolve_min_chars(jsonl_files, min_chars)

        total_exchanges = sum(
            len(parse_exchanges(jsonl, min_chars=resolved_min_chars))
            for jsonl in jsonl_files
        )

        if total_exchanges > 0:
            skip_count, skip_strategy = _resolve_skip_count(
                total_exchanges, skip_existing, distill_limit
            )

            distill_count = total_exchanges - skip_count
            if distill_count > 0:
                run_distill_now = _ask_run_distill_now(distill_count)

    # --- 実行フェーズ（ここから DB・ファイルを作成） ---
    init_db(db)

    config_path = db.parent / "config.toml"
    if not config_path.exists():
        config_path.write_text(
            "# Codeatrium configuration\n"
            "\n"
            "[distill]\n"
            '# model = "claude-haiku-4-5-20251001"\n'
            "# batch_limit = 20\n"
            "# min_chars = 100   # この文字数未満の exchange は蒸留スキップ\n"
            "\n"
            "[index]\n"
            "# min_chars = 50   # trivial フィルタ閾値（文字数）\n"
        )

    typer.echo(f"Initialized: {db}")

    from codeatrium.cli.prime_cmd import inject_claude_md

    if inject_claude_md(root):
        typer.echo(f"Updated: {root / 'CLAUDE.md'} (codeatrium section)")

    if total_exchanges == 0:
        return

    actual_total = 0
    for jsonl in jsonl_files:
        actual_total += index_file(jsonl, db, min_chars=resolved_min_chars)

    typer.echo(f"Indexed {actual_total} existing exchange(s).")

    if skip_count > 0:
        order_clause = (
            "ORDER BY LENGTH(user_content) + LENGTH(agent_content) ASC"
            if skip_strategy == "longest"
            else "ORDER BY ply_start ASC"
        )
        con = get_connection(db)
        con.execute(
            f"""
            UPDATE exchanges SET distilled_at = 'skipped'
            WHERE distilled_at IS NULL
            AND id IN (
                SELECT id FROM exchanges
                WHERE distilled_at IS NULL
                {order_clause}
                LIMIT ?
            )
            """,
            (skip_count,),
        )
        con.commit()
        con.close()
        remaining = actual_total - skip_count
        typer.echo(f"Marked {skip_count} exchange(s) as skipped. {remaining} will be distilled.")
    else:
        typer.echo(f"All {actual_total} exchange(s) will be distilled.")

    if run_distill_now:
        from codeatrium.config import load_config
        from codeatrium.distiller import distill_all

        cfg = load_config(root)
        typer.echo("Running distillation...")
        def _on_progress(cur: int, tot: int, error: str | None = None) -> None:
            if error:
                typer.echo(f"  [{cur}/{tot}] error: {error}", err=True)
            else:
                typer.echo(f"  [{cur}/{tot}] distilled", err=True)

        count = distill_all(
            db,
            model=cfg.distill_model,
            on_progress=_on_progress,
            project_root=str(root),
            distill_min_chars=cfg.distill_min_chars,
        )
        typer.echo(f"Distilled {count} exchange(s).")


_MIN_CHARS_CANDIDATES = [50, 100, 200, 500]


def _count_exchanges_by_threshold(
    jsonl_files: list[Path], thresholds: list[int]
) -> dict[int, int]:
    """各閾値ごとの exchange 件数をカウントする。min_chars=0 で全件パースして集計。"""
    from codeatrium.indexer import parse_exchanges

    # 全 exchange の combined 文字数を収集
    lengths: list[int] = []
    for jsonl in jsonl_files:
        for ex in parse_exchanges(jsonl, min_chars=0):
            lengths.append(len(ex.user_content) + len(ex.agent_content))

    return {t: sum(1 for length in lengths if length >= t) for t in thresholds}


def _resolve_min_chars(
    jsonl_files: list[Path], min_chars_flag: int | None
) -> int:
    """init 時の min_chars を決定する。フラグ指定済みならそのまま、未指定なら対話。"""
    if min_chars_flag is not None:
        return min_chars_flag

    counts = _count_exchanges_by_threshold(jsonl_files, _MIN_CHARS_CANDIDATES)

    # exchange が0件なら対話不要
    if counts.get(_MIN_CHARS_CANDIDATES[0], 0) == 0:
        return _MIN_CHARS_CANDIDATES[0]

    typer.echo("\nMin chars threshold for existing exchanges:")
    for i, threshold in enumerate(_MIN_CHARS_CANDIDATES, 1):
        label = " (default)" if threshold == 50 else ""
        typer.echo(f"  [{i}] {threshold} chars{label} — {counts[threshold]} exchanges")
    typer.echo(f"  [{len(_MIN_CHARS_CANDIDATES) + 1}] Custom")

    choice = typer.prompt("Choice", default="1")

    for i, threshold in enumerate(_MIN_CHARS_CANDIDATES, 1):
        if choice == str(i):
            return threshold

    if choice == str(len(_MIN_CHARS_CANDIDATES) + 1):
        return typer.prompt("Min chars threshold?", type=int)

    typer.echo("Invalid choice. Using default (50).")
    return 50


def _ask_run_distill_now(distill_count: int) -> bool:
    """蒸留を今すぐ実行するか聞く。"""
    typer.echo(
        f"\nStart distillation now? ({distill_count} exchanges, uses claude --print)"
    )
    typer.echo("  [1] No — distill on next session start (default)")
    typer.echo("  [2] Yes — run now")

    choice = typer.prompt("Choice", default="1")
    return choice == "2"


def _ask_distill_priority() -> str:
    """蒸留対象の優先順位を選択する。"""
    typer.echo("\nDistill priority:")
    typer.echo("  [1] Recent — newest exchanges first")
    typer.echo("  [2] Longest — longest exchanges first")

    choice = typer.prompt("Choice", default="1")

    if choice == "2":
        return "longest"
    return "recent"


def _resolve_skip_count(
    total: int, skip_existing: bool, distill_limit: int | None
) -> tuple[int, str]:
    """スキップする exchange 数と優先順位を決定する。フラグ or 対話プロンプト。

    Returns: (skip_count, strategy) where strategy is "recent" or "longest"
    """
    if skip_existing:
        return total, "recent"
    if distill_limit is not None:
        skip = max(0, total - distill_limit)
        strategy = _ask_distill_priority() if skip > 0 else "recent"
        return skip, strategy

    # 対話プロンプト
    typer.echo(
        f"\nFound {total} existing exchanges from past sessions.\n"
        "Distillation uses claude --print (Haiku) and consumes tokens.\n"
        "⚠ Skipped exchanges cannot be distilled later.\n"
    )
    typer.echo("How should existing exchanges be handled?")
    typer.echo("  [1] Skip all — only distill future sessions")
    typer.echo(f"  [2] Distill last {DEFAULT_DISTILL_RECENT} — recent history only")
    typer.echo(f"  [3] Distill all — {total} exchanges (token consumption)")
    typer.echo("  [4] Custom — specify how many exchanges to distill")

    choice = typer.prompt("Choice", default="1")

    if choice == "1":
        return total, "recent"
    elif choice == "3":
        return 0, "recent"

    if choice == "2":
        skip = max(0, total - DEFAULT_DISTILL_RECENT)
    elif choice == "4":
        n = typer.prompt("How many exchanges to distill?", type=int)
        skip = max(0, total - n)
    else:
        typer.echo("Invalid choice. Skipping all existing exchanges.")
        return total, "recent"

    strategy = _ask_distill_priority() if skip > 0 else "recent"
    return skip, strategy


app.command()(index)
app.command()(distill)
app.command()(search)
app.command()(context)
app.command()(status)
app.command()(show)
app.command()(dump)
app.command()(prime)
app.add_typer(hook_app, name="hook")
app.add_typer(server_app, name="server")
