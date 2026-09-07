"""Regression coverage for issue #46's process-level GIT_* leak.

Production code that shells out to `git` during a test (e.g.
`file_renames._run_git_follow`, `core.ingest._git_blob_near`,
`eval.gen.gen_symbol_recall`'s `git ls-files`/`git log`) must never resolve
against a GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_PREFIX/GIT_COMMON_DIR
leaked into the whole pytest PROCESS by an enclosing git hook — only
against the `cwd`/`project_root` it was explicitly given.

This is distinct from what test_file_renames.py/test_core_ingest.py/
test_db.py/test_eval_cmd.py already cover: those only prove their own
`run_git`-based setup calls are isolated. The leak this file defends
against lands on the pytest PROCESS's environment *before any test runs*
(a hook exports it into `make check`'s entire child process tree,
including pytest itself), so reproducing it requires spawning a genuinely
separate pytest process with those variables set — a mid-test
`monkeypatch.setenv()` would not reproduce it, since `conftest.py`'s
`pytest_configure` guard only runs once, at that process's own session
start.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import run_git

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_external_repo(root: Path) -> Path:
    """A throwaway repo standing in for the outer real repository a git
    hook would leak GIT_DIR/GIT_WORK_TREE/... from. Its state is recorded
    and re-checked byte-for-byte after each nested run below."""
    repo = root / "external"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "external@real.example")
    run_git(repo, "config", "user.name", "ExternalReal")
    (repo / "real.txt").write_text("do not touch\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "external initial commit")
    return repo


def _snapshot(repo: Path) -> tuple[str, str]:
    """(.git/config contents, `git log --oneline`) — this test's own
    process already had its GIT_* env stripped by `pytest_configure` at
    session start, so a plain inherited-env `git` call here is safe."""
    config = (repo / ".git" / "config").read_text()
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return config, log


def _leaked_env(external_repo: Path) -> dict[str, str]:
    """Env a pre-commit/pre-push hook would export into `make check`'s
    entire child process tree, pointed at `external_repo`."""
    return {
        **os.environ,
        "GIT_DIR": str(external_repo / ".git"),
        "GIT_WORK_TREE": str(external_repo),
        "GIT_INDEX_FILE": str(external_repo / ".git" / "index"),
        "GIT_PREFIX": "",
        "GIT_COMMON_DIR": str(external_repo / ".git"),
    }


def _run_nested_pytest(
    node_id: str, leaked_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Spawn a fresh pytest process — simulating the hook-invoked one —
    with the leaked GIT_* vars already set on its environment, exactly as
    a pre-commit/pre-push hook would set them before ever launching
    `make check`."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", node_id, "-p", "no:cacheprovider", "-q"],
        cwd=_REPO_ROOT,
        env=leaked_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_file_renames_git_log_follow_ignores_leaked_process_git_dir(
    tmp_path: Path,
) -> None:
    """`file_renames.resolve_aliases` (via `_run_git_follow`) must resolve
    rename history against the tmp fixture repo the nested test builds —
    not a GIT_DIR leaked into the whole pytest process — and must never
    write to the leaked repo either."""
    external_repo = _make_external_repo(tmp_path)
    before = _snapshot(external_repo)

    result = _run_nested_pytest(
        "tests/test_file_renames.py::test_resolve_aliases_finds_renamed_file",
        _leaked_env(external_repo),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(external_repo) == before


def test_core_ingest_git_blob_near_ignores_leaked_process_git_dir(
    tmp_path: Path,
) -> None:
    """`core.ingest._git_blob_near` must read the historical blob from the
    tmp fixture repo the nested test builds, not a leaked GIT_DIR."""
    external_repo = _make_external_repo(tmp_path)
    before = _snapshot(external_repo)

    result = _run_nested_pytest(
        "tests/test_core_ingest.py::"
        "test_ingest_resolves_symbols_against_touch_time_git_blob_not_live_disk",
        _leaked_env(external_repo),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(external_repo) == before


def test_eval_gen_symbol_recall_git_calls_ignore_leaked_process_git_dir(
    tmp_path: Path,
) -> None:
    """`eval.gen.gen_symbol_recall`'s `git ls-files`/`git log --follow`
    calls must enumerate and read history from the tmp fixture repo the
    nested test builds, not a leaked GIT_DIR."""
    external_repo = _make_external_repo(tmp_path)
    before = _snapshot(external_repo)

    result = _run_nested_pytest(
        "tests/test_eval_cmd.py::test_eval_gen_writes_symbol_recall_dataset",
        _leaked_env(external_repo),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(external_repo) == before
