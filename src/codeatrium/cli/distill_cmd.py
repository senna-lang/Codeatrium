"""loci distill コマンド — backend/provider サポート"""

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
    import fcntl
    import os

    from codeatrium.config import load_config
    from codeatrium.distiller import distill_all
    from codeatrium.llm import DistillBackend
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    cfg = load_config(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    lock_path = db.parent / "distill.lock"

    # ロック取得: fcntl.flock で排他ロック（LOCK_NB: 非ブロッキング）
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        typer.echo("loci distill is already running. Exiting.", err=True)
        raise typer.Exit(0)

    def _on_progress(cur: int, tot: int, error: str | None = None) -> None:
        if error:
            typer.echo(f"  [{cur}/{tot}] error: {error}", err=True)
        else:
            typer.echo(f"  [{cur}/{tot}] distilled", err=True)

    try:
        from codeatrium.db import check_drift

        drifts = check_drift(db)
        for key, recorded, current in drifts:
            typer.echo(
                f"[warn] {key} changed ({recorded} -> {current}). Re-index recommended.",
                err=True,
            )

        count, err_count = distill_all(
            db,
            limit=limit,
            backend=DistillBackend.from_config(cfg),
            on_progress=_on_progress,
            project_root=str(root),
            distill_min_chars=cfg.distill_min_chars,
        )
        typer.echo(f"Distilled {count} exchange(s).")
        if err_count > 0:
            typer.echo(f"{err_count} exchange(s) failed — see errors above.", err=True)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
