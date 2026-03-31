"""loci CLI エントリポイント — サブコマンド登録のみ"""

from __future__ import annotations

import typer

from codeatrium.cli.distill_cmd import distill
from codeatrium.cli.hook_cmd import hook_app
from codeatrium.cli.index_cmd import index
from codeatrium.cli.search_cmd import context, search
from codeatrium.cli.server_cmd import server_app
from codeatrium.cli.show_cmd import dump, show
from codeatrium.cli.status_cmd import status

app = typer.Typer(help="CLI-first memory layer for AI coding agents")


@app.command()
def init() -> None:
    """プロジェクトルートに .codeatrium/memory.db を初期化する"""
    from codeatrium.db import init_db
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

    if db.exists():
        typer.echo(f"Already initialized: {db}")
        return

    init_db(db)
    typer.echo(f"Initialized: {db}")


app.command()(index)
app.command()(distill)
app.command()(search)
app.command()(context)
app.command()(status)
app.command()(show)
app.command()(dump)
app.add_typer(hook_app, name="hook")
app.add_typer(server_app, name="server")
