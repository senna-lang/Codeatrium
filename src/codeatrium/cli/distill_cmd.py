"""loci distill コマンド"""

from __future__ import annotations

from typing import Annotated

import typer


def distill(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", help="処理する最大件数（省略時は全件）"),
    ] = None,
) -> None:
    """未蒸留の exchange を claude -p で蒸留して palace_objects を生成する"""
    import os

    from codeatrium.distiller import distill_all
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    lock_path = db.parent / "distill.lock"
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            os.kill(existing_pid, 0)
            typer.echo(
                f"loci distill is already running (PID {existing_pid}). Exiting.",
                err=True,
            )
            raise typer.Exit(0)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    lock_path.write_text(str(os.getpid()))
    try:
        count = distill_all(db, limit=limit)
        typer.echo(f"Distilled {count} exchange(s).")
    finally:
        lock_path.unlink(missing_ok=True)
