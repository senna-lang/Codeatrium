"""loci hook install コマンド"""

from __future__ import annotations

from typing import Annotated

import typer

hook_app = typer.Typer(help="Harness hook 管理")


@hook_app.command("install")
def hook_install(
    harness: Annotated[
        str, typer.Option("--harness", help="対象 harness（既定: claude）")
    ] = "claude",
) -> None:
    """Harness の lifecycle automation を設定する。"""
    from codeatrium.adapters.harness.hooks import hooks_for
    from codeatrium.config import load_config
    from codeatrium.paths import find_project_root

    root = find_project_root()
    cfg = load_config(root)
    try:
        _changed, message = hooks_for(
            harness, batch_limit=cfg.distill_batch_limit
        ).install(root)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--harness") from exc
    typer.echo(message)


@hook_app.command("uninstall")
def hook_uninstall(
    harness: Annotated[
        str, typer.Option("--harness", help="対象 harness（既定: claude）")
    ] = "claude",
) -> None:
    """Harness の native lifecycle automation を解除する。"""
    from codeatrium.adapters.harness.hooks import hooks_for

    try:
        _changed, message = hooks_for(harness).uninstall()
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--harness") from exc
    typer.echo(message)
