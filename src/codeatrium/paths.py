"""パス解決ヘルパー: プロジェクトルート・DB パス・Claude セッションログパスの解決"""

from __future__ import annotations

import subprocess
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEATRIUM_DIR = ".codeatrium"
DB_NAME = "memory.db"


def git_root() -> Path | None:
    """git rev-parse --show-toplevel でリポジトリルートを返す。git 外/未インストールなら None"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def find_project_root() -> Path:
    """.codeatrium/ を探してプロジェクトルートを返す。
    検索順: cwd → 親ディレクトリ（git root まで）
    git root を超えて遡らないことでプロジェクト外の DB を拾わない。
    """
    cwd = Path.cwd()
    root = git_root()
    if root:
        candidates = [cwd, *cwd.parents]
        for p in candidates:
            if (p / CODEATRIUM_DIR).exists():
                return p
            if p == root:
                break
        return root
    else:
        # git 外: cwd のみ探索（親を遡ると別プロジェクトの DB を拾う）
        if (cwd / CODEATRIUM_DIR).exists():
            return cwd
        return cwd


def db_path(project_root: Path) -> Path:
    return project_root / CODEATRIUM_DIR / DB_NAME


def resolve_claude_projects_path(project_root: Path) -> Path | None:
    """project_root から対応する ~/.claude/projects/<hash>/ を解決する。
    Claude Code はパスの "/" を "-" に変換したディレクトリ名を使う。
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    candidates = [project_root, Path.cwd()]
    for base in candidates:
        dir_name = str(base).replace("/", "-")
        candidate = CLAUDE_PROJECTS_DIR / dir_name
        if candidate.exists() and any(candidate.rglob("*.jsonl")):
            return candidate
    return None


def sock_path(project_root: Path) -> Path:
    return db_path(project_root).parent / "embedder.sock"


def server_pid_path(project_root: Path) -> Path:
    return db_path(project_root).parent / "embedder.pid"


def loci_bin() -> str:
    """sys.executable と同じ venv の bin/loci のフルパスを返す（PATH 非依存）。"""
    import sys

    return str(Path(sys.executable).parent / "loci")
