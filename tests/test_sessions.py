"""Canonical session storage contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from codeatrium.db import get_connection, init_db


def test_legacy_conversation_backfills_canonical_session(
    tmp_path: Path,
) -> None:
    """A legacy exchange gains reconstructable Claude provenance."""
    db_path = tmp_path / "memory.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            started_at TEXT,
            last_ply_end INT NOT NULL DEFAULT -1
        );
        CREATE TABLE exchanges (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            ply_start INT NOT NULL,
            ply_end INT NOT NULL,
            user_content TEXT NOT NULL,
            agent_content TEXT NOT NULL
        );
        INSERT INTO conversations VALUES (
            'legacy', '/tmp/session.jsonl', NULL, 4
        );
        INSERT INTO exchanges VALUES (
            'exchange', 'legacy', 2, 4, 'user', 'agent'
        );
        PRAGMA user_version = 10;
        """
    )
    con.close()

    init_db(db_path)

    con = get_connection(db_path)
    session = con.execute(
        "SELECT id, harness, source_session_id, cursor FROM sessions"
    ).fetchone()
    exchange = con.execute(
        """
        SELECT harness, session_ref, source_session_id, source_turn_id,
               session_id
        FROM exchanges WHERE id = 'exchange'
        """
    ).fetchone()
    con.close()

    assert tuple(session[1:]) == ("claude", "/tmp/session.jsonl", "v1:ply:4")
    assert tuple(exchange) == (
        "claude",
        "/tmp/session.jsonl#ply=2-4",
        "/tmp/session.jsonl",
        "2",
        session["id"],
    )
