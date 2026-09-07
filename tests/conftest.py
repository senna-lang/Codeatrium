"""Shared pytest helpers and process-wide guards for tests that shell out to
nested `git` subprocesses.

Git hooks (pre-commit/pre-push) export `GIT_DIR`/`GIT_WORK_TREE`/
`GIT_INDEX_FILE`/`GIT_PREFIX`/`GIT_COMMON_DIR` into their entire child
process tree. When `make check` (and therefore pytest) runs *from inside*
such a hook, the leaked variables land on the pytest PROCESS itself, before
any test ever runs — not just on subprocess calls a test explicitly makes.
Any code path reached during a test — test setup helpers *and* production
code (e.g. `file_renames._run_git_follow`, `core.ingest._git_blob_near`)
that shells out to `git` under an explicit `cwd`/`-C` without overriding
`env` — inherits that leaked state, and an explicit `GIT_DIR` always wins
over `cwd`-based repository discovery. Left unfixed, this makes such calls
silently operate on the outer real repository instead of the intended
`tmp_path` fixture, corrupting its config/index (issue #46).

Two layers close this:

1. `pytest_configure` strips every inherited `GIT_*` variable (except the
   allowlisted commit-date overrides below) from `os.environ` once, at
   session start, before collection or any test runs — no legitimate test
   should depend on inheriting the outer process's git-dir selection. This
   protects every git-shelling code path, known or future, not just the
   ones that route through `run_git`.
2. `run_git` additionally filters `GIT_*` out of whatever base environment
   (ambient `os.environ` or a caller-supplied override dict) a test setup
   helper explicitly passes to a nested `git` invocation, so a test can
   still safely override `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` for
   deterministic commit timestamps without resurrecting the discovery
   variables layer 1 already removed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Variables a test may legitimately want to pin (deterministic commit dates).
# Everything else starting with GIT_ is hook-inherited state that must never
# leak into a nested git call scoped to a tmp_path fixture.
_GIT_ENV_ALLOWLIST = {"GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"}


def _drop_git_discovery_vars(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if not key.startswith("GIT_") or key in _GIT_ENV_ALLOWLIST
    }


def pytest_configure(config: pytest.Config) -> None:
    """Strip hook-leaked GIT_* discovery vars from this pytest process's
    environment for the whole session, before any test runs (issue #46
    follow-up). Without this, a `git` call made *anywhere* during a test
    — including inside production code, not just a test's own setup
    helper — would still resolve against a hook-leaked GIT_DIR instead of
    the `cwd`/`project_root` it was explicitly pointed at."""
    for key in [k for k in os.environ if k.startswith("GIT_") and k not in _GIT_ENV_ALLOWLIST]:
        del os.environ[key]


def run_git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    """Run `git <args>` in `cwd`, isolated from any inherited GIT_* state."""
    base_env = os.environ if env is None else env
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_drop_git_discovery_vars(base_env),
    )
