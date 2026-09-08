"""`loci gc` command for safe local memory-database maintenance.

It requires an initialized project database, applies pending forward migrations,
then delegates deletion, snapshot retention, and VACUUM to the DB layer.
"""

from __future__ import annotations

import typer


def gc() -> None:
    """Remove orphaned data, retain rollback backups, and compact the database."""
    from codeatrium.db import init_db
    from codeatrium.maintenance import collect_garbage
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    init_db(db)
    result = collect_garbage(db)
    typer.echo(
        "GC complete: "
        f"palace_objects={result.palace_objects}, "
        f"rooms={result.rooms}, "
        f"vec_palace={result.vec_palace}, "
        f"code_touches={result.code_touches}, "
        f"code_edges={result.code_edges}, "
        f"exchange_files={result.exchange_files}, "
        f"sessions={result.sessions}, "
        f"backup_archives={result.backup_archives}."
    )
