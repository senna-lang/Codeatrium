"""loci CLI エントリポイント — サブコマンド登録のみ"""

from __future__ import annotations

from typing import Annotated

import typer

from codeatrium.cli.distill_cmd import distill
from codeatrium.cli.hook_cmd import hook_app
from codeatrium.cli.index_cmd import index
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
) -> None:
    """プロジェクトルートに .codeatrium/memory.db を初期化する"""
    from codeatrium.db import get_connection, init_db
    from codeatrium.indexer import index_file
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

    init_db(db)

    # サンプル config を生成
    config_path = db.parent / "config.toml"
    if not config_path.exists():
        config_path.write_text(
            "# Codeatrium configuration\n"
            "\n"
            "[distill]\n"
            '# model = "claude-haiku-4-5-20251001"\n'
            "# batch_limit = 20\n"
        )

    typer.echo(f"Initialized: {db}")

    # 既存セッションを自動インデックス
    target_dir = resolve_claude_projects_path(root)
    if target_dir is None:
        return

    jsonl_files = list(target_dir.rglob("*.jsonl"))
    if not jsonl_files:
        return

    total_exchanges = 0
    for jsonl in jsonl_files:
        total_exchanges += index_file(jsonl, db)

    if total_exchanges == 0:
        return

    typer.echo(f"Indexed {total_exchanges} existing exchange(s).")

    # 蒸留対象の決定
    skip_count = _resolve_skip_count(
        total_exchanges, skip_existing, distill_limit
    )

    if skip_count > 0:
        con = get_connection(db)
        con.execute(
            """
            UPDATE exchanges SET distilled_at = 'skipped'
            WHERE distilled_at IS NULL
            AND id IN (
                SELECT id FROM exchanges
                WHERE distilled_at IS NULL
                ORDER BY ply_start ASC
                LIMIT ?
            )
            """,
            (skip_count,),
        )
        con.commit()
        con.close()
        skipped = skip_count
        remaining = total_exchanges - skipped
        typer.echo(f"Marked {skipped} exchange(s) as skipped. {remaining} will be distilled.")
    else:
        typer.echo(f"All {total_exchanges} exchange(s) will be distilled.")


def _resolve_skip_count(
    total: int, skip_existing: bool, distill_limit: int | None
) -> int:
    """スキップする exchange 数を決定する。フラグ or 対話プロンプト。"""
    if skip_existing:
        return total
    if distill_limit is not None:
        return max(0, total - distill_limit)

    # 対話プロンプト
    typer.echo(
        f"\nFound {total} existing exchanges from past sessions.\n"
        "Distillation uses claude --print (Haiku) and consumes tokens.\n"
    )
    typer.echo("How should existing exchanges be handled?")
    typer.echo("  [1] Skip all — only distill future sessions")
    typer.echo(f"  [2] Distill last {DEFAULT_DISTILL_RECENT} — recent history only")
    typer.echo(f"  [3] Distill all — {total} exchanges (token consumption)")
    typer.echo("  [4] Custom — specify how many recent exchanges to distill")

    choice = typer.prompt("Choice", default="1")

    if choice == "1":
        return total
    elif choice == "2":
        return max(0, total - DEFAULT_DISTILL_RECENT)
    elif choice == "3":
        return 0
    elif choice == "4":
        n = typer.prompt("How many recent exchanges to distill?", type=int)
        return max(0, total - n)
    else:
        typer.echo("Invalid choice. Skipping all existing exchanges.")
        return total


app.command()(index)
app.command()(distill)
app.command()(search)
app.command()(context)
app.command()(status)
app.command()(show)
app.command()(dump)
app.add_typer(hook_app, name="hook")
app.add_typer(server_app, name="server")
