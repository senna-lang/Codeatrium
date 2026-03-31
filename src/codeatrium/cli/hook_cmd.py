"""loci hook install コマンド"""

from __future__ import annotations

import typer

hook_app = typer.Typer(help="Claude Code hook 管理")


@hook_app.command("install")
def hook_install() -> None:
    """Claude Code の Stop / SessionStart フックに loci を登録する。

    Stop (async):      loci index    — 毎ターン・ノンブロッキング
    SessionStart:      loci distill  — CC起動・/clear・/resume・compact 時
                       claude --print サブセッションは SessionStart を発火しないためループなし
    """
    from codeatrium.hooks import install_hooks

    _changed, message = install_hooks()
    typer.echo(message)
