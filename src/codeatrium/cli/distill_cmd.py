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

    from codeatrium.config import load_config
    from codeatrium.distiller import distill_all
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    cfg = load_config(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    lock_path = db.parent / "distill.lock"

    # ロック取得: O_CREAT | O_EXCL で原子的に作成（TOCTOU 防止）
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # 既存ロックのプロセスが生きているか確認
        try:
            existing_pid = int(lock_path.read_text().strip())
            os.kill(existing_pid, 0)
            typer.echo(
                f"loci distill is already running (PID {existing_pid}). Exiting.",
                err=True,
            )
            raise typer.Exit(0)
        except (ValueError, ProcessLookupError, PermissionError):
            # stale lock — 再取得
            lock_path.unlink(missing_ok=True)
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
            except FileExistsError:
                typer.echo("loci distill: lost lock race after stale cleanup. Exiting.", err=True)
                raise typer.Exit(0)

    def _on_progress(cur: int, tot: int, error: str | None = None) -> None:
        if error:
            typer.echo(f"  [{cur}/{tot}] error: {error}", err=True)
        else:
            typer.echo(f"  [{cur}/{tot}] distilled", err=True)

    try:
        count = distill_all(
            db,
            limit=limit,
            model=cfg.distill_model,
            on_progress=_on_progress,
            project_root=str(root),
            distill_min_chars=cfg.distill_min_chars,
        )
        typer.echo(f"Distilled {count} exchange(s).")
    finally:
        lock_path.unlink(missing_ok=True)
