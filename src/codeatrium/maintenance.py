"""Database maintenance for `loci gc`.

The command preserves every row reachable from a live exchange, removes only
unreferenced persistence rows, snapshots the database before mutation, and
runs SQLite VACUUM after the cleanup transaction has committed.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codeatrium.db import get_connection

_BACKUP_ARCHIVE_RETENTION = 3


@dataclass(frozen=True)
class GarbageCollectionResult:
    """Observable counts from one completed database-maintenance run."""

    palace_objects: int
    rooms: int
    vec_palace: int
    code_touches: int
    code_edges: int
    exchange_files: int
    sessions: int
    backup_archives: int


def _current_backup_path(db_path: Path) -> Path:
    """Return the one rollback snapshot path associated with a database."""
    return db_path.with_name(f"{db_path.name}.bak")


def _archive_backup(current_backup: Path) -> None:
    """Move the previous rollback snapshot aside without overwriting history."""
    if not current_backup.exists():
        return

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    archive = current_backup.with_name(f"{current_backup.name}.{timestamp}")
    suffix = 1
    while archive.exists():
        archive = current_backup.with_name(
            f"{current_backup.name}.{timestamp}.{suffix}"
        )
        suffix += 1
    current_backup.replace(archive)


def _prune_backup_archives(current_backup: Path) -> int:
    """Keep the newest bounded set of archived snapshots beside `.bak`."""
    archives = sorted(
        current_backup.parent.glob(f"{current_backup.name}.*"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    removed = 0
    for archive in archives[_BACKUP_ARCHIVE_RETENTION:]:
        archive.unlink()
        removed += 1
    return removed


def _create_backup(con: sqlite3.Connection, db_path: Path) -> Path:
    """Create a consistent SQLite snapshot before destructive maintenance."""
    current_backup = _current_backup_path(db_path)
    _archive_backup(current_backup)

    backup_con = sqlite3.connect(current_backup)
    try:
        con.backup(backup_con)
    finally:
        backup_con.close()
    os.chmod(current_backup, 0o600)
    return current_backup


def _delete(con: sqlite3.Connection, statement: str) -> int:
    """Run a single cleanup delete and return the number of affected rows."""
    con.execute(statement)
    return con.execute("SELECT changes()").fetchone()[0]


def collect_garbage(db_path: Path) -> GarbageCollectionResult:
    """Remove only DB rows with no live parent, back up, and VACUUM safely.

    A rollback snapshot is made before cleanup. SQLite cannot run VACUUM inside
    a transaction, so orphan deletion commits first; VACUUM then compacts the
    committed database. Old archive generations are pruned only after both
    operations succeed.
    """
    con = get_connection(db_path)
    try:
        current_backup = _create_backup(con, db_path)
        con.execute("BEGIN")
        try:
            palace_objects = _delete(
                con,
                """DELETE FROM palace_objects
                   WHERE NOT EXISTS (
                       SELECT 1 FROM exchanges WHERE exchanges.id = palace_objects.exchange_id
                   )""",
            )
            rooms = _delete(
                con,
                """DELETE FROM rooms
                   WHERE NOT EXISTS (
                       SELECT 1 FROM palace_objects WHERE palace_objects.id = rooms.palace_object_id
                   )""",
            )
            vec_palace = _delete(
                con,
                """DELETE FROM vec_palace
                   WHERE NOT EXISTS (
                       SELECT 1 FROM palace_objects WHERE palace_objects.id = vec_palace.palace_id
                   )""",
            )
            code_touches = _delete(
                con,
                """DELETE FROM code_touches
                   WHERE NOT EXISTS (
                       SELECT 1 FROM exchanges WHERE exchanges.id = code_touches.exchange_id
                   )""",
            )
            code_edges = _delete(
                con,
                """DELETE FROM code_edges
                   WHERE NOT EXISTS (
                       SELECT 1 FROM exchanges WHERE exchanges.id = code_edges.exchange_id
                   )""",
            )
            exchange_files = _delete(
                con,
                """DELETE FROM exchange_files
                   WHERE NOT EXISTS (
                       SELECT 1 FROM exchanges WHERE exchanges.id = exchange_files.exchange_id
                   )""",
            )
            sessions = _delete(
                con,
                """DELETE FROM sessions
                   WHERE NOT EXISTS (
                       SELECT 1 FROM exchanges WHERE exchanges.session_id = sessions.id
                   )""",
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

        con.execute("VACUUM")
        backup_archives = _prune_backup_archives(current_backup)
        return GarbageCollectionResult(
            palace_objects=palace_objects,
            rooms=rooms,
            vec_palace=vec_palace,
            code_touches=code_touches,
            code_edges=code_edges,
            exchange_files=exchange_files,
            sessions=sessions,
            backup_archives=backup_archives,
        )
    finally:
        con.close()
