"""loci status コマンド"""

from __future__ import annotations

import json
from typing import Annotated

import typer


def status(
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """インデックス状態（exchange 数・蒸留済み数・DB サイズ）を表示する"""
    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)

    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    total = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    distilled = con.execute(
        "SELECT COUNT(*) FROM exchanges WHERE distilled_at IS NOT NULL"
    ).fetchone()[0]
    palace_count = con.execute("SELECT COUNT(*) FROM palace_objects").fetchone()[0]
    symbol_count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    con.close()

    db_size_bytes = db.stat().st_size
    db_size_kb = db_size_bytes / 1024

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "db_path": str(db),
                    "exchanges": total,
                    "distilled": distilled,
                    "undistilled": total - distilled,
                    "palace_objects": palace_count,
                    "symbols": symbol_count,
                    "db_size_kb": round(db_size_kb, 1),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"DB: {db} ({db_size_kb:.1f} KB)")
        typer.echo(
            f"Exchanges : {total} total, {distilled} distilled, {total - distilled} pending"
        )
        typer.echo(f"Palace    : {palace_count}")
        typer.echo(f"Symbols   : {symbol_count}")
