"""loci distill コマンド — client registry 経由で ModelClient を解決して蒸留する"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from codeatrium.adapters.model.types import ModelClient


def _is_interactive() -> bool:
    """stdin/stdout が両方 TTY かどうか（テストで monkeypatch する用の切り出し）"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_client_list(statuses, recommended: str | None) -> None:
    for i, s in enumerate(statuses, start=1):
        mark = " (recommended)" if s.id == recommended else ""
        typer.echo(f"  {i}. {s.label} [{s.id}]{mark}")


def prompt_client_selection(root) -> ModelClient | None:
    """discover → setup offer → Ready 一覧 → 選択。

    config は書き換えず ModelClient を返すだけ（runtime reselect は once — save は
    `loci distill --setup` のみ）。
    """
    from codeatrium.adapters.model.registry import (
        discover,
        ready_clients,
        recommended_id,
        resolve_client,
        setup,
    )
    from codeatrium.config import load_config

    statuses = discover()
    for s in statuses:
        if s.state == "setupable":
            typer.echo(f"{s.label}: {s.reason}")
            if typer.confirm(f"Set up {s.label} now?", default=False):
                ok, msg = setup(s.id)
                typer.echo(msg)
                if ok:
                    statuses = discover()

    ready = ready_clients(statuses)
    if not ready:
        typer.echo("No distill client is ready. Run `loci distill --setup` later.")
        return None

    rec = recommended_id(statuses)
    _print_client_list(ready, rec)
    default_idx = next((i for i, s in enumerate(ready, 1) if s.id == rec), 1)
    raw = typer.prompt("Select client", default=str(default_idx)).strip()
    idx = int(raw) if raw.isdigit() and 1 <= int(raw) <= len(ready) else default_idx
    chosen = ready[idx - 1]
    return chosen.client or resolve_client(chosen.id, load_config(root))


def _setup_and_save(root) -> None:
    """`loci distill --setup`: 選んだ client を config.toml に書き込む"""
    from codeatrium.adapters.model.registry import write_client_config

    client = prompt_client_selection(root)
    if client is None:
        raise typer.Exit(1)

    config_path = root / ".codeatrium" / "config.toml"
    write_client_config(config_path, client)
    typer.echo(f"Saved distill.client = {client.id}")


def _resolve_backend(cfg, root, is_tty: bool):
    """cfg から DistillBackend を解決する。

    unconfigured/not-ready のとき: TTY なら一度限りの再選択（config は書かない）、
    非対話なら None を返し呼び出し側が warn+skip する（silent auto-switch 禁止）。
    """
    from codeatrium.adapters.model.registry import check_ready
    from codeatrium.llm import DistillBackend, DistillUnconfiguredError

    def _from_client(client: ModelClient) -> DistillBackend:
        return DistillBackend(
            provider=client.provider, model=client.model, base_url=client.base_url
        )

    try:
        backend = DistillBackend.from_config(cfg)
    except DistillUnconfiguredError:
        if not is_tty:
            typer.echo(
                "Distill client is not configured. Run `loci distill --setup`.",
                err=True,
            )
            return None
        typer.echo("Distill client is not configured.")
        client = prompt_client_selection(root)
        return _from_client(client) if client else None

    assert cfg.distill_client is not None  # from_config succeeded => configured
    status = check_ready(cfg.distill_client)
    if status.state == "ready":
        return backend

    if not is_tty:
        typer.echo(
            f"Configured distill client '{cfg.distill_client}' is not ready "
            f"({status.reason}). Not switching automatically — run "
            "`loci distill --setup` or `loci distill` interactively.",
            err=True,
        )
        return None

    typer.echo(
        f"Configured client '{cfg.distill_client}' is not ready: {status.reason}"
    )
    client = prompt_client_selection(root)
    return _from_client(client) if client else None


def distill(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", help="処理する最大件数（省略時は全件）"),
    ] = None,
    setup: Annotated[
        bool,
        typer.Option(
            "--setup",
            help="distill client を discover/setup/選択して config に保存する",
        ),
    ] = False,
) -> None:
    """未蒸留の exchange を distill client で蒸留して palace_objects を生成する"""
    import fcntl
    import os

    from codeatrium.config import load_config
    from codeatrium.distiller import distill_all
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    if setup:
        _setup_and_save(root)
        return

    cfg = load_config(root)
    is_tty = _is_interactive()

    lock_path = db.parent / "distill.lock"

    # ロック取得: fcntl.flock で排他ロック（LOCK_NB: 非ブロッキング）
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        typer.echo("loci distill is already running. Exiting.", err=True)
        raise typer.Exit(0)

    backend = _resolve_backend(cfg, root, is_tty)
    if backend is None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise typer.Exit(0 if not is_tty else 1)

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
            backend=backend,
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
