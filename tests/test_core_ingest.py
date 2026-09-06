"""Canonical adapter output persists without harness-specific core logic."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from codeatrium.core.ingest import _git_blob_near, ingest_parse_result
from codeatrium.core.models import (
    CanonicalExchange,
    CanonicalSession,
    ExchangeArtifacts,
    ParseResult,
)
from codeatrium.db import get_connection, init_db
from codeatrium.models import CodeTouch, FileOnly, LineRange


def test_ingest_persists_provenance_cursor_and_files(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    session = CanonicalSession(
        harness="codex",
        source_session_id="session-1",
        primary_ref="/tmp/rollout.jsonl",
        project_key=str(tmp_path),
    )
    result = ParseResult(
        exchanges=(
            CanonicalExchange(
                harness="codex",
                session_ref="/tmp/rollout.jsonl#ply=2-4",
                source_session_id="session-1",
                source_turn_id="turn-2",
                ply_start=2,
                ply_end=4,
                user_content="add a canonical persistence path",
                agent_content="implemented it",
                files_touched=("src/codeatrium/core/ingest.py",),
                agent_model="gpt-5",
                agent_provider="openai",
            ),
        ),
        next_cursor="v1:ply:4",
    )

    con = get_connection(db_path)
    assert ingest_parse_result(con, session, result) == 1
    con.commit()
    assert ingest_parse_result(con, session, result) == 0
    con.commit()
    stored = con.execute(
        """
        SELECT e.harness, e.session_ref, e.source_session_id, e.source_turn_id,
               e.agent_model, e.agent_provider, s.cursor
        FROM exchanges e JOIN sessions s ON s.id = e.session_id
        """
    ).fetchone()
    files = con.execute("SELECT file_path FROM exchange_files").fetchall()
    con.close()

    assert tuple(stored) == (
        "codex",
        "/tmp/rollout.jsonl#ply=2-4",
        "session-1",
        "turn-2",
        "gpt-5",
        "openai",
        "v1:ply:4",
    )
    assert [row[0] for row in files] == ["src/codeatrium/core/ingest.py"]


def test_ingest_persists_parent_session_ref_on_new_conversation(tmp_path: Path) -> None:
    """design §2.3・§4.2: CanonicalSession.parent_session_ref を conversations に書き込む"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    session = CanonicalSession(
        harness="claude",
        source_session_id="agent-sub-1",
        primary_ref="/tmp/proj/uuid1/subagents/agent-sub-1.jsonl",
        project_key=str(tmp_path),
        parent_session_ref="/tmp/proj/uuid1.jsonl",
    )
    result = ParseResult(
        exchanges=(
            CanonicalExchange(
                harness="claude",
                session_ref="/tmp/proj/uuid1/subagents/agent-sub-1.jsonl#ply=0-1",
                source_session_id="agent-sub-1",
                source_turn_id="turn-0",
                ply_start=0,
                ply_end=1,
                user_content="apply edit-3 per plan.json",
                agent_content="done",
            ),
        ),
        next_cursor="v1:ply:1",
    )

    con = get_connection(db_path)
    assert ingest_parse_result(con, session, result) == 1
    con.commit()
    stored = con.execute(
        "SELECT parent_session_ref FROM conversations WHERE source_path = ?",
        (session.primary_ref,),
    ).fetchone()
    con.close()

    assert stored[0] == "/tmp/proj/uuid1.jsonl"


def test_ingest_conversation_without_parent_session_ref_stays_null(tmp_path: Path) -> None:
    """親を持たない通常セッションは parent_session_ref が NULL のまま（既定の後方互換）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    session = CanonicalSession(
        harness="codex",
        source_session_id="session-1",
        primary_ref="/tmp/rollout.jsonl",
        project_key=str(tmp_path),
    )
    result = ParseResult(
        exchanges=(
            CanonicalExchange(
                harness="codex",
                session_ref="/tmp/rollout.jsonl#ply=0-1",
                source_session_id="session-1",
                source_turn_id="turn-0",
                ply_start=0,
                ply_end=1,
                user_content="hello",
                agent_content="hi",
            ),
        ),
        next_cursor="v1:ply:1",
    )

    con = get_connection(db_path)
    ingest_parse_result(con, session, result)
    con.commit()
    stored = con.execute(
        "SELECT parent_session_ref FROM conversations WHERE source_path = ?",
        (session.primary_ref,),
    ).fetchone()
    con.close()

    assert stored[0] is None


def test_ingest_persists_exchange_scoped_code_touches(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    source = tmp_path / "src" / "target.py"
    source.parent.mkdir()
    source.write_text("def target() -> None:\n    pass\n")
    session = CanonicalSession(
        harness="codex",
        source_session_id="session-1",
        primary_ref="/tmp/rollout.jsonl",
        project_key=str(tmp_path),
    )
    result = ParseResult(
        exchanges=(
            CanonicalExchange(
                harness="codex",
                session_ref="/tmp/rollout.jsonl#ply=2-4",
                source_session_id="session-1",
                source_turn_id="turn-2",
                ply_start=2,
                ply_end=4,
                user_content="update the target function",
                agent_content="updated target",
            ),
        ),
        next_cursor="v1:ply:4",
        artifacts=(
            ExchangeArtifacts(
                source_turn_id="turn-2",
                code_touches=(
                    CodeTouch(
                        harness="codex",
                        tool_call_id="call-1",
                        file_path=str(source),
                        touch_kind="edit",
                        locators=(FileOnly(),),
                        added=1,
                        removed=0,
                        ts=None,
                    ),
                ),
            ),
        ),
    )

    con = get_connection(db_path)
    assert ingest_parse_result(con, session, result) == 1
    con.commit()
    touch_count = con.execute("SELECT COUNT(*) FROM code_touches").fetchone()[0]
    edge_count = con.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
    symbol_count = con.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0]
    con.close()

    assert touch_count == 1
    assert edge_count == 1
    assert symbol_count == 1


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def test_ingest_resolves_symbols_against_touch_time_git_blob_not_live_disk(
    tmp_path: Path,
) -> None:
    """A touch's line range must be checked against the file as it looked at
    touch time, not the live/current file — regression for the drift bug
    where `code_symbols` only ever reflected the newest on-disk snapshot."""
    project_root = tmp_path
    _git(project_root, "init")
    _git(project_root, "config", "user.email", "t@t.com")
    _git(project_root, "config", "user.name", "T")

    src = project_root / "src.py"
    src.write_text("def foo():\n    pass\n")
    _git(project_root, "add", ".")
    old_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00",
    }
    _git(project_root, "commit", "-m", "old", env=old_env)

    # Grow the file so `foo` moves far down — simulates months of later edits
    # that shift line numbers well past where this touch originally landed.
    padding = "\n".join(f"x{i} = {i}" for i in range(100))
    src.write_text(f"{padding}\n\ndef foo():\n    pass\n")
    _git(project_root, "add", ".")
    new_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-06-01T00:00:00",
        "GIT_COMMITTER_DATE": "2026-06-01T00:00:00",
    }
    _git(project_root, "commit", "-m", "new", env=new_env)

    db_path = project_root / "memory.db"
    init_db(db_path)
    session = CanonicalSession(
        harness="claude",
        source_session_id="s1",
        primary_ref="/tmp/x.jsonl",
        project_key=str(project_root),
    )
    result = ParseResult(
        exchanges=(
            CanonicalExchange(
                harness="claude",
                session_ref="/tmp/x.jsonl#ply=0-1",
                source_session_id="s1",
                source_turn_id="turn-1",
                ply_start=0,
                ply_end=1,
                user_content="touch foo at old time",
                agent_content="done",
            ),
        ),
        next_cursor="v1:ply:1",
        artifacts=(
            ExchangeArtifacts(
                source_turn_id="turn-1",
                code_touches=(
                    CodeTouch(
                        harness="claude",
                        tool_call_id="call-1",
                        file_path=str(src),
                        touch_kind="edit",
                        locators=(
                            LineRange(old_start=1, old_lines=2, new_start=1, new_lines=2),
                        ),
                        added=2,
                        removed=0,
                        ts="2026-01-01T00:00:00",  # matches the OLD commit only
                    ),
                ),
            ),
        ),
    )

    con = get_connection(db_path)
    assert ingest_parse_result(con, session, result) == 1
    con.commit()
    edge = con.execute("SELECT granularity, symbol_id FROM code_edges").fetchone()
    con.close()

    assert edge["granularity"] == "line"
    assert edge["symbol_id"] is not None


def test_git_blob_near_returns_none_outside_a_git_repo(tmp_path: Path) -> None:
    assert _git_blob_near(tmp_path, "src.py", "2026-01-01T00:00:00") is None


def test_git_blob_near_returns_none_without_a_timestamp(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    assert _git_blob_near(tmp_path, "src.py", None) is None


def test_git_blob_near_falls_back_to_after_when_no_earlier_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "T")
    src = tmp_path / "src.py"
    src.write_text("def only():\n    pass\n")
    _git(tmp_path, "add", ".")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-06-01T00:00:00",
        "GIT_COMMITTER_DATE": "2026-06-01T00:00:00",
    }
    _git(tmp_path, "commit", "-m", "only", env=env)

    # Requested time is BEFORE the only commit — must fall back to --after.
    blob = _git_blob_near(tmp_path, "src.py", "2026-01-01T00:00:00")

    assert blob == b"def only():\n    pass\n"
