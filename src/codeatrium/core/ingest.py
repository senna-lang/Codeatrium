"""Persistence of adapter-neutral sessions and exchanges."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from codeatrium.code_touches import (
    build_code_touch_rows,
    normalize_repo_path,
    touches_to_edges,
)
from codeatrium.core.models import CanonicalSession, ParseResult
from codeatrium.resolver import Symbol, SymbolResolver
from codeatrium.utils import sha256

_GIT_TIMEOUT_S = 10


def _git_blob_near(project_root: Path, rel_path: str, ts: str | None) -> bytes | None:
    """Best-effort git blob for `rel_path` as of `ts` (nearest commit before, else after).

    `code_touches.new_start/new_lines` are frozen at the moment of that edit;
    resolving symbols against the *live* working-tree file (as of index time)
    drifts as the file keeps evolving after the touch, so old touches stop
    overlapping any current symbol boundary. Resolving against the git blob
    nearest the touch's own timestamp keeps the touch and the symbol layout
    it's compared against from the same point in time.

    Returns `None` on any failure (not a git repo, file untracked at that
    time, git unavailable, timeout) so callers fall back to the live file —
    this can only improve alignment over that fallback, never regress it.
    """
    if not ts:
        return None
    for order in ("--before", "--after"):
        try:
            result = subprocess.run(
                [
                    "git", "-C", str(project_root), "log", "-n", "1",
                    f"{order}={ts}", "--format=%H", "--", rel_path,
                ],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        sha = result.stdout.strip()
        if not sha:
            continue
        try:
            blob = subprocess.run(
                ["git", "-C", str(project_root), "show", f"{sha}:{rel_path}"],
                capture_output=True,
                timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if blob.returncode == 0:
            return blob.stdout
    return None


def _resolve_symbols_at(
    resolver: SymbolResolver, project_root: Path, touch_file_path: str, rel_path: str, ts: str | None
) -> list[Symbol]:
    """Resolve symbols as of `ts` via git, falling back to the live disk file."""
    source = _git_blob_near(project_root, rel_path, ts)
    if source is not None:
        return resolver.extract_source(source, rel_path)
    return resolver.extract(Path(touch_file_path))


def ingest_parse_result(
    con: sqlite3.Connection,
    session: CanonicalSession,
    result: ParseResult,
) -> int:
    """Persist one adapter result and its opaque cursor in one transaction."""
    persisted_session_id = sha256(
        f"{session.harness}:{session.source_session_id}"
    )
    now = datetime.now(UTC).isoformat()
    con.execute(
        """
        INSERT INTO sessions (
            id, harness, source_session_id, primary_ref, project_key, cursor,
            cursor_version, started_at, title, git_branch_last, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(harness, source_session_id) DO UPDATE SET
            primary_ref = excluded.primary_ref,
            project_key = excluded.project_key,
            cursor = excluded.cursor,
            started_at = COALESCE(sessions.started_at, excluded.started_at),
            title = COALESCE(excluded.title, sessions.title),
            git_branch_last = COALESCE(
                excluded.git_branch_last, sessions.git_branch_last
            ),
            updated_at = excluded.updated_at
        """,
        (
            persisted_session_id,
            session.harness,
            session.source_session_id,
            session.primary_ref,
            session.project_key,
            result.next_cursor,
            session.started_at,
            session.title,
            session.git_branch_last,
            now,
        ),
    )
    # Keep legacy query consumers working while sessions becomes authoritative.
    # `conversations.source_path` is UNIQUE and is what legacy indexers keyed
    # on (sha256(str(path)), no harness prefix) — that id can differ from
    # `persisted_session_id`. Resolve the existing row by source_path first so
    # re-indexing a file already known under a legacy id does not violate the
    # UNIQUE(source_path) constraint.
    existing_conversation = con.execute(
        "SELECT id FROM conversations WHERE source_path = ?",
        (session.primary_ref,),
    ).fetchone()
    if existing_conversation is not None:
        conversation_id = existing_conversation["id"]
    else:
        conversation_id = persisted_session_id
        con.execute(
            """
            INSERT INTO conversations (id, source_path, started_at, last_ply_end)
            VALUES (?, ?, ?, -1)
            """,
            (conversation_id, session.primary_ref, session.started_at),
        )

    inserted = 0
    inserted_ids: dict[str, str] = {}
    for exchange in result.exchanges:
        exchange_id = sha256(
            ":".join(
                (
                    exchange.harness,
                    exchange.source_session_id,
                    exchange.source_turn_id,
                )
            )
        )
        existing = con.execute(
            "SELECT id FROM exchanges WHERE canonical_exchange_id = ? OR id = ?",
            (exchange_id, exchange_id),
        ).fetchone()
        cursor = (
            con.execute(
                """
                INSERT INTO exchanges (
                    id, canonical_exchange_id, conversation_id, ply_start, ply_end,
                    user_content, agent_content, git_branch, session_id, harness,
                    session_ref, source_session_id, source_turn_id, agent_model,
                    agent_provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exchange_id,
                    exchange_id,
                    conversation_id,
                    exchange.ply_start,
                    exchange.ply_end,
                    exchange.user_content,
                    exchange.agent_content,
                    exchange.git_branch,
                    persisted_session_id,
                    exchange.harness,
                    exchange.session_ref,
                    exchange.source_session_id,
                    exchange.source_turn_id,
                    exchange.agent_model,
                    exchange.agent_provider,
                ),
            )
            if existing is None
            else None
        )
        if cursor is not None:
            inserted += 1
            inserted_ids[exchange.source_turn_id] = exchange_id
        for file_path in exchange.files_touched:
            con.execute(
                (
                    "INSERT OR IGNORE INTO exchange_files "
                    "(exchange_id, file_path) VALUES (?, ?)"
                ),
                (exchange_id, file_path),
            )
    _persist_artifacts(
        con,
        result,
        inserted_ids,
        Path(session.project_key),
    )
    if result.exchanges:
        con.execute(
            "UPDATE conversations SET last_ply_end = MAX(last_ply_end, ?) WHERE id = ?",
            (result.exchanges[-1].ply_end, conversation_id),
        )
    return inserted


def _persist_artifacts(
    con: sqlite3.Connection,
    result: ParseResult,
    exchange_ids: dict[str, str],
    project_root: Path,
) -> None:
    """Persist adapter-provided edit artifacts for newly inserted exchanges."""
    if not exchange_ids or not project_root.is_dir():
        return

    resolver = SymbolResolver()
    symbol_cache: dict[tuple[str, str | None], list[Symbol]] = {}
    resolved_at = datetime.now(UTC).isoformat()
    for artifacts in result.artifacts:
        exchange_id = exchange_ids.get(artifacts.source_turn_id)
        if exchange_id is None:
            continue
        for rename in artifacts.file_renames:
            old_path = normalize_repo_path(str(rename.old_path), str(project_root))
            new_path = normalize_repo_path(str(rename.new_path), str(project_root))
            if old_path is None or new_path is None:
                continue
            con.execute(
                """
                INSERT INTO file_renames (old_path, new_path, source, ts)
                VALUES (?, ?, 'harness', ?)
                ON CONFLICT(old_path, new_path) DO UPDATE SET
                    source = excluded.source,
                    ts = excluded.ts
                """,
                (old_path, new_path, rename.ts),
            )

        for touch in artifacts.code_touches:
            rel_path = normalize_repo_path(touch.file_path, str(project_root))
            if rel_path is None:
                continue
            for touch_row in build_code_touch_rows(
                touch, exchange_id=exchange_id, rel_file_path=rel_path
            ):
                con.execute(
                    """
                    INSERT OR IGNORE INTO code_touches
                        (id, exchange_id, harness, tool_call_id, file_path,
                         touch_kind, locator_kind, old_start, old_lines,
                         new_start, new_lines, old_string, new_string,
                         added, removed, ts)
                    VALUES (:id, :exchange_id, :harness, :tool_call_id,
                            :file_path, :touch_kind, :locator_kind, :old_start,
                            :old_lines, :new_start, :new_lines, :old_string,
                            :new_string, :added, :removed, :ts)
                    """,
                    touch_row,
                )
            cache_key = (rel_path, touch.ts)
            symbols = symbol_cache.get(cache_key)
            if symbols is None:
                symbols = _resolve_symbols_at(
                    resolver, project_root, touch.file_path, rel_path, touch.ts
                )
                symbol_cache[cache_key] = symbols
            for symbol in symbols:
                symbol_id = sha256(f"{rel_path}:{symbol.symbol_name}")
                con.execute(
                    """
                    INSERT OR REPLACE INTO code_symbols
                        (id, file_path, symbol_name, symbol_kind, signature,
                         line, end_line, lang, resolved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol_id,
                        rel_path,
                        symbol.symbol_name,
                        symbol.symbol_kind,
                        symbol.signature,
                        symbol.line,
                        symbol.end_line,
                        symbol.lang,
                        resolved_at,
                    ),
                )
            for edge in touches_to_edges(
                touch,
                exchange_id=exchange_id,
                rel_file_path=rel_path,
                symbols=symbols,
            ):
                con.execute(
                    """
                    INSERT INTO code_edges
                        (id, exchange_id, file_path, symbol_id, edge_kind,
                         granularity, confidence, added, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET added = added + excluded.added
                    """,
                    (
                        edge.id,
                        edge.exchange_id,
                        edge.file_path,
                        edge.symbol_id,
                        edge.edge_kind,
                        edge.granularity,
                        edge.confidence,
                        edge.added,
                        edge.ts,
                    ),
                )
