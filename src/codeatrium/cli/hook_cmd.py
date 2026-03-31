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
    from codeatrium.config import load_config
    from codeatrium.hooks import install_hooks
    from codeatrium.paths import find_project_root

    cfg = load_config(find_project_root())
    _changed, message = install_hooks(batch_limit=cfg.distill_batch_limit)
    typer.echo(message)
