"""Shared pytest helpers for tests that shell out to nested `git` subprocesses.

Git hooks (pre-commit/pre-push) export `GIT_DIR`/`GIT_WORK_TREE`/
`GIT_INDEX_FILE`/`GIT_PREFIX`/`GIT_COMMON_DIR` into their entire child
process tree. When `make check` (and therefore pytest) runs *from inside*
such a hook, any test that shells out to `git` in a `tmp_path` fixture
inherits those variables and silently operates on the outer real repository
instead of the fixture — corrupting its config/index (issue #46).

`run_git` strips every inherited `GIT_*` variable (except
`GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`, which some tests deliberately pin
to get reproducible commit timestamps) before every nested `git`
invocation, so nested git commands can only ever see the `cwd` passed in.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Variables a test may legitimately want to pin (deterministic commit dates).
# Everything else starting with GIT_ is hook-inherited state that must never
# leak into a nested git call scoped to a tmp_path fixture.
_GIT_ENV_ALLOWLIST = {"GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"}


def run_git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    """Run `git <args>` in `cwd`, isolated from any inherited GIT_* state."""
    base_env = os.environ if env is None else env
    clean_env = {
        key: value
        for key, value in base_env.items()
        if not key.startswith("GIT_") or key in _GIT_ENV_ALLOWLIST
    }
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env=clean_env
    )
