"""Persistence of adapter-neutral sessions and exchanges."""

from __future__ import annotations

import bisect
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


def _git_log_for_path(project_root: Path, rel_path: str) -> list[tuple[float, str]]:
    """Full commit history touching `rel_path`, oldest→newest, as (epoch, sha).

    One subprocess call per unique file — the batched counterpart to
    `_git_blob_near`'s per-(file, ts) `git log --before=`/`--after=` pair,
    used by `_batch_git_blobs_near` to resolve many timestamps against the
    same file without repeating the `git log` spawn for each one (issue #25).
    Returns `[]` on any failure (not a git repo, git unavailable, timeout),
    the same best-effort contract as `_git_blob_near`.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "log", "--format=%H %cI", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    commits: list[tuple[float, str]] = []
    for line in result.stdout.splitlines():
        sha, _, iso_date = line.partition(" ")
        if not sha or not iso_date:
            continue
        try:
            epoch = datetime.fromisoformat(iso_date).timestamp()
        except ValueError:
            continue
        commits.append((epoch, sha))
    commits.sort(key=lambda c: c[0])
    return commits


def _nearest_sha(commits: list[tuple[float, str]], ts_epoch: float) -> str | None:
    """Same selection `_git_blob_near` makes one file at a time: the latest
    commit at or before `ts_epoch`, else (only reachable when every commit
    postdates it) the newest commit overall — mirrors git's own `-n 1
    --after=` picking the most recent match in its default newest-first
    order once `--before=` found nothing."""
    if not commits:
        return None
    epochs = [c[0] for c in commits]
    idx = bisect.bisect_right(epochs, ts_epoch)
    return commits[idx - 1][1] if idx > 0 else commits[-1][1]


def _git_show_blob(project_root: Path, rel_path: str, sha: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "show", f"{sha}:{rel_path}"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _batch_git_blobs_near(
    project_root: Path, requests: set[tuple[str, str | None]]
) -> dict[tuple[str, str | None], bytes | None]:
    """Batched counterpart to `_git_blob_near` for many (rel_path, ts) pairs.

    Fetches each unique file's commit history once via `_git_log_for_path`
    instead of a `git log --before=`/`--after=` pair per (file, ts), and
    dedupes `git show` calls by resolved sha. A historical backfill over N
    touches spanning a handful of files and commits costs O(unique files +
    unique blobs) subprocess spawns instead of O(N) (issue #25).
    """
    blobs: dict[tuple[str, str | None], bytes | None] = {}
    by_path: dict[str, list[str | None]] = {}
    for rel_path, ts in requests:
        by_path.setdefault(rel_path, []).append(ts)

    show_cache: dict[tuple[str, str], bytes | None] = {}
    for rel_path, ts_list in by_path.items():
        commits = _git_log_for_path(project_root, rel_path)
        for ts in ts_list:
            if not ts or not commits:
                blobs[(rel_path, ts)] = None
                continue
            try:
                ts_epoch = datetime.fromisoformat(ts).timestamp()
            except ValueError:
                blobs[(rel_path, ts)] = None
                continue
            sha = _nearest_sha(commits, ts_epoch)
            if sha is None:
                blobs[(rel_path, ts)] = None
                continue
            show_key = (rel_path, sha)
            if show_key not in show_cache:
                show_cache[show_key] = _git_show_blob(project_root, rel_path, sha)
            blobs[(rel_path, ts)] = show_cache[show_key]

    return blobs


def _batch_resolve_symbols_at(
    resolver: SymbolResolver,
    project_root: Path,
    requests: set[tuple[str, str | None]],
) -> dict[tuple[str, str | None], list[Symbol]]:
    """Batched counterpart to `_resolve_symbols_at` for the historical
    touch-time backfill (`db._backfill_touch_time_symbol_edges`), which
    resolves many (file, ts) pairs up front rather than one touch at a
    time. Falls back to the live disk file per-request, same as
    `_resolve_symbols_at`, when no git blob is available."""
    blobs = _batch_git_blobs_near(project_root, requests)
    resolved: dict[tuple[str, str | None], list[Symbol]] = {}
    for key in requests:
        rel_path, _ts = key
        blob = blobs.get(key)
        resolved[key] = (
            resolver.extract_source(blob, rel_path)
            if blob is not None
            else resolver.extract(project_root / rel_path)
        )
    return resolved


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
            INSERT INTO conversations (id, source_path, started_at, last_ply_end, parent_session_ref)
            VALUES (?, ?, ?, -1, ?)
            """,
            (conversation_id, session.primary_ref, session.started_at, session.parent_session_ref),
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
            touch_rows = build_code_touch_rows(
                touch, exchange_id=exchange_id, rel_file_path=rel_path
            )
            touch_is_new = False
            for touch_row in touch_rows:
                cur = con.execute(
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
                if cur.rowcount > 0:
                    touch_is_new = True
            if not touch_is_new:
                # Every row this touch would produce already exists — a
                # duplicate replay of an edit already recorded (e.g. an
                # adapter re-emitting the same tool-call event within one
                # parse). Its contribution to code_symbols/code_edges was
                # already applied the first time it was seen; redoing it
                # here would double-count `added` (see ON CONFLICT below,
                # which sums across *distinct* touches on purpose).
                continue
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
