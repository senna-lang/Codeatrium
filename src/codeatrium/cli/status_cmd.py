"""loci status コマンド"""

from __future__ import annotations

import json
from typing import Annotated

import typer


def status(
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """インデックス状態（exchange 数・蒸留済み数・DB サイズ）を表示する"""
    from codeatrium.adapters.model.registry import check_ready
    from codeatrium.config import load_config
    from codeatrium.db import check_drift, get_connection
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    total = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    distilled = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distill_status = 'distilled'"
    ).fetchone()[0]
    skipped = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distill_status = 'skipped'"
    ).fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distill_status = 'pending'"
    ).fetchone()[0]
    palace_count = con.execute("SELECT COUNT(*) FROM palace_objects").fetchone()[0]
    symbol_count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    con.close()

    cfg = load_config(root)
    if cfg.distill_unconfigured or not cfg.distill_client:
        distill_client_label = "unconfigured"
        distill_available = False
    else:
        distill_client_label = cfg.distill_client
        distill_available = check_ready(cfg.distill_client).state == "ready"

    # config.toml の構文エラーは background hook からは stderr が
    # `> /dev/null 2>&1` に捨てられ気づけないため、`loci status` で明示的に
    # 警告する（load_config は既定へフォールバックしただけで、それ自体は
    # distill_unconfigured と区別が付かない）。
    if cfg.config_error:
        typer.echo(
            f"⚠ config.toml failed to parse, running unconfigured: {cfg.config_error}",
            err=True,
        )

    drifts = check_drift(db)
    for key, recorded, current in drifts:
        typer.echo(
            f"[drift] {key}: recorded={recorded}, current={current} — re-index recommended",
            err=True,
        )

    db_size_bytes = db.stat().st_size
    db_size_kb = db_size_bytes / 1024

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "db_path": str(db),
                    "config_error": cfg.config_error,
                    "exchanges": total,
                    "distilled": distilled,
                    "skipped": skipped,
                    "pending": pending,
                    "palace_objects": palace_count,
                    "symbols": symbol_count,
                    "db_size_kb": round(db_size_kb, 1),
                    "distill_client": distill_client_label,
                    "distill_available": distill_available,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"DB: {db} ({db_size_kb:.1f} KB)")
        typer.echo(
            f"Exchanges : {total} total | {distilled} distilled, {skipped} skipped, {pending} pending"
        )
        typer.echo(f"Palace    : {palace_count}")
        typer.echo(f"Symbols   : {symbol_count}")
        avail = "ready" if distill_available else "not ready"
        typer.echo(f"Distill   : {distill_client_label} ({avail})")
        if cfg.config_error:
            typer.echo(f"Config    : ⚠ parse error — {cfg.config_error}")
