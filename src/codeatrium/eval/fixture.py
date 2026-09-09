"""Deterministic synthetic project for the symbol-recall CI gate (issue #37).

Builds a tiny git repo plus seeded `code_symbols`/`code_edges` so the
`symbol` adapter can run with no network, no embedding model, and no real
dogfood corpus (privacy: the live `.codeatrium/memory.db` is never public).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.eval.datasets.schema import Query
from codeatrium.paths import db_path

_GIT_ENV_ALLOWLIST = {"GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"}

WIDGET_SOURCE = """\
def list_dir():
    return []


def sort_items(items):
    return sorted(items)
"""

FIXTURE_QUERIES: list[Query] = [
    Query(
        id="q-list-dir",
        kind="symbol",
        value="src/widget.py::list_dir",
        gold_exchange_ids=("ex-list-dir",),
    ),
    Query(
        id="q-sort-items",
        kind="symbol",
        value="src/widget.py::sort_items",
        gold_exchange_ids=("ex-sort-items",),
    ),
]


def _git_env() -> dict[str, str]:
    """Strip hook-leaked GIT_* discovery vars (same contract as tests/conftest.py)."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in _GIT_ENV_ALLOWLIST
    }
    env.setdefault("GIT_AUTHOR_NAME", "codeatrium-fixture")
    env.setdefault("GIT_AUTHOR_EMAIL", "fixture@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "codeatrium-fixture")
    env.setdefault("GIT_COMMITTER_EMAIL", "fixture@example.com")
    return env


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_git_env(),
    )


def build_symbol_recall_fixture(project_root: Path) -> Path:
    """Create a git-backed synthetic project and return its memory.db path."""
    src = project_root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "widget.py").write_text(WIDGET_SOURCE, encoding="utf-8")

    _run_git(project_root, "init")
    _run_git(project_root, "config", "user.email", "fixture@example.com")
    _run_git(project_root, "config", "user.name", "codeatrium-fixture")
    _run_git(project_root, "config", "commit.gpgsign", "false")
    _run_git(project_root, "add", ".")
    _run_git(project_root, "commit", "-m", "seed symbol-recall fixture")

    db = db_path(project_root)
    init_db(db)
    con = get_connection(db)
    try:
        con.execute("INSERT INTO conversations (id, source_path) VALUES ('c1', '/p')")
        con.execute(
            "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) "
            "VALUES ('ex-list-dir', 'c1', 0, 1, 'fix list_dir', 'done')"
        )
        con.execute(
            "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) "
            "VALUES ('ex-sort-items', 'c1', 2, 3, 'fix sort_items', 'done')"
        )
        con.execute(
            "INSERT INTO code_symbols (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at) "
            "VALUES ('sym-list-dir', 'src/widget.py', 'list_dir', 'function', 'def list_dir():', 1, 2, '.py', '2026-01-01')"
        )
        con.execute(
            "INSERT INTO code_symbols (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at) "
            "VALUES ('sym-sort-items', 'src/widget.py', 'sort_items', 'function', 'def sort_items(items):', 4, 5, '.py', '2026-01-01')"
        )
        con.execute(
            "INSERT INTO code_edges (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts) "
            "VALUES ('edge-list-dir', 'ex-list-dir', 'src/widget.py', 'sym-list-dir', 'edit', 'line', 1.0, 3, '2026-01-01')"
        )
        con.execute(
            "INSERT INTO code_edges (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts) "
            "VALUES ('edge-sort-items', 'ex-sort-items', 'src/widget.py', 'sym-sort-items', 'edit', 'line', 1.0, 2, '2026-01-02')"
        )
        con.commit()
    finally:
        con.close()
    return db
