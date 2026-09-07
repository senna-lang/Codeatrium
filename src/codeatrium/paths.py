"""パス解決ヘルパー: プロジェクトルート・DB パス・Claude セッションログパスの解決"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

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


def find_project_root(notify: bool = True) -> Path:
    """.codeatrium/ を探してプロジェクトルートを返す。
    検索順: cwd → 親ディレクトリ（git root まで）
    git root を超えて遡らないことでプロジェクト外の DB を拾わない。

    cwd 以外（親）で `.codeatrium/` を発見した場合は stderr に注意書きを出す。
    意図せず親プロジェクトの DB を共有していることに気付けるようにするため。
    notify=False で抑止可能（テスト・機械処理用途）。

    cwd は resolve() してから git_root()（実パス）と比較する。symlink 配下の
    cwd を未解決のまま使うと、`p == root` の break 条件が一致せず走査が
    git root を超えて続き、リポジトリ外の `.codeatrium/` を拾いうる（issue #27）。
    """
    cwd = Path.cwd().resolve()
    root = git_root()
    if root:
        candidates = [cwd, *cwd.parents]
        for p in candidates:
            if (p / CODEATRIUM_DIR).exists():
                if notify and p != cwd:
                    print(
                        f"Note: using .codeatrium/ from parent directory: {p}",
                        file=sys.stderr,
                    )
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
    Claude Code はパス中の英数字以外の文字（"/" や "." を含む）をすべて "-" に
    変換したディレクトリ名を使う。"/" のみ変換すると、パスに "." を含む
    プロジェクト（例: バージョン付きディレクトリ名）で実際のセッション
    ディレクトリと一致しない（issue #27）。
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    candidates = [project_root, Path.cwd()]
    for base in candidates:
        dir_name = re.sub(r"[^a-zA-Z0-9]", "-", str(base))
        candidate = CLAUDE_PROJECTS_DIR / dir_name
        if candidate.exists() and any(candidate.rglob("*.jsonl")):
            return candidate
    return None


def resolve_codex_sessions_path() -> Path | None:
    """Codex rollout JSONL を格納する既定セッションディレクトリを返す。"""
    path = Path.home() / ".codex" / "sessions"
    if path.exists() and any(path.rglob("rollout-*.jsonl")):
        return path
    return None


def resolve_opencode_db_path() -> Path | None:
    """OpenCode のセッション SQLite DB（~/.local/share/opencode/opencode.db）を返す。"""
    path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return path if path.exists() else None


def resolve_omp_pi_sessions_path(project_root: Path) -> Path | None:
    """project_root に対応する ~/.omp/agent/sessions/<slug>/ を解決する。

    omp-pi は Claude Code と同様にプロジェクトパスからディレクトリ名を作るが、
    ホームディレクトリからの相対パスを "-" でつないだ形になる
    （/Users/x/Documents/Repos/foo → -Documents-Repos-foo）。
    """
    sessions_dir = Path.home() / ".omp" / "agent" / "sessions"
    if not sessions_dir.exists():
        return None

    for base in (project_root, Path.cwd()):
        try:
            relative = base.relative_to(Path.home())
        except ValueError:
            continue
        candidate = sessions_dir / ("-" + "-".join(relative.parts))
        if candidate.exists() and any(candidate.glob("*.jsonl")):
            return candidate
    return None


def resolve_grok_sessions_path(project_root: Path) -> Path | None:
    """project_root に対応する ~/.grok/sessions/<percent-encoded-path>/ を解決する。

    grok は作業ディレクトリの絶対パスをそのまま percent-encode した
    （`/` も `%2F` に変換する）ディレクトリ名を使う。実体のログはさらにその下の
    `<session-uuid>/updates.jsonl` にあるため、存在確認は再帰で行う。
    """
    sessions_dir = Path.home() / ".grok" / "sessions"
    if not sessions_dir.exists():
        return None

    for base in (project_root, Path.cwd()):
        candidate = sessions_dir / quote(str(base), safe="")
        if candidate.exists() and any(candidate.rglob("updates.jsonl")):
            return candidate
    return None


SOCK_FILENAME = "embedder.sock"
PID_FILENAME = "embedder.pid"


def sock_path(project_root: Path) -> Path:
    return db_path(project_root).parent / SOCK_FILENAME


def server_pid_path(project_root: Path) -> Path:
    return db_path(project_root).parent / PID_FILENAME


def loci_bin() -> str:
    """loci バイナリのフルパスを返す。
    まず sys.executable と同じ venv の bin/loci を探し、無ければ PATH 上の
    `loci`（shutil.which）にフォールバックする。venv 外（pipx/global install）
    での実行では venv 内に loci が存在しないため（issue #27）。
    どちらも見つからない場合は venv パスを返しつつ stderr に警告する。
    """
    venv_bin = Path(sys.executable).parent / "loci"
    if venv_bin.exists():
        return str(venv_bin)

    which_bin = shutil.which("loci")
    if which_bin:
        return which_bin

    print(
        f"Warning: could not locate `loci` binary (checked {venv_bin} and PATH); "
        "hooks referencing it may fail to run",
        file=sys.stderr,
    )
    return str(venv_bin)
