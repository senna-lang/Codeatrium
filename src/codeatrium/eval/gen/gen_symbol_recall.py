"""symbol-recall dataset generation — git-grounded, `symbols`-table-free (H1).

Gold for a symbol S is built from three independent, non-circular signals:

1. `code_touches` — which files an exchange *edited* (Edit/Write/MultiEdit),
   populated directly from harness tool-call paths at index time,
   independent of tree-sitter. Deliberately not `exchange_files`, which also
   records mere file *reads* — `loci context <file>:<symbol>` promises
   git-blame semantics ("who edited this"), not "who ever looked at this".
   Building gold from reads-included data measured >85% false negatives
   against exchanges the feature was never meant to surface.
2. A literal token match for S in the exchange's *raw* verbatim text.
3. Optionally, git history: which branches contain a commit whose diff for
   S's file mentions S, intersected with the exchange's own `git_branch`.

Crucially, gold construction never reads `symbols`/`code_symbols`/`code_edges`
— those are populated by the exact mechanism under test
(`eval/adapters/symbol.py`). Using them to build gold would make H1 self-
confirming. Tree-sitter is used only to *enumerate* which symbol names exist
in the current HEAD checkout (a fact about the code, not about codeatrium's
historical resolution of it).

`enumerate_head_symbols` / `_load_file_history` / `_branches_containing` are
the only impure (git/tree-sitter I/O) functions here; `gold_for_symbol` and
`generate_symbol_recall_queries(..., allowed_branches_fn=...)` are unit-
testable against an in-memory DB with fake git info injected.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codeatrium.code_touches import normalize_repo_path
from codeatrium.eval.datasets.schema import Query
from codeatrium.resolver import Symbol, SymbolResolver
from codeatrium.utils import sha256

DATASET_NAME = "symbol-recall"

_SUPPORTED_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".go"})
_GIT_TIMEOUT_S = 60

AllowedBranchesFn = Callable[[Path, str, str], "frozenset[str] | None"]


# ---- git-tracked HEAD symbol enumeration (impure) ----


def _git_tracked_files(project_root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [line for line in result.stdout.splitlines() if line]


def enumerate_head_symbols(project_root: Path) -> list[Symbol]:
    """Every function/class/method tree-sitter finds in HEAD's tracked files."""
    resolver = SymbolResolver()
    symbols: list[Symbol] = []
    for rel_path in _git_tracked_files(project_root):
        if Path(rel_path).suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        symbols.extend(resolver.extract(project_root / rel_path))
    return symbols


# ---- git branch grounding (impure, best-effort) ----


@dataclass(frozen=True)
class _FileHistory:
    """Per-commit diff bodies for one file, keyed by commit sha."""

    commit_diffs: dict[str, str]


def _load_file_history(project_root: Path, rel_file_path: str) -> _FileHistory | None:
    """`None` means "could not determine" — callers must not treat that as excluded."""
    marker = "\x01"
    try:
        result = subprocess.run(
            [
                "git", "-C", str(project_root), "log", "--all", "--follow",
                f"--format={marker}%H", "-p", "--", rel_file_path,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    commit_diffs: dict[str, str] = {}
    for chunk in result.stdout.split(marker):
        if not chunk:
            continue
        sha, _, body = chunk.partition("\n")
        sha = sha.strip()
        if sha:
            commit_diffs[sha] = body
    return _FileHistory(commit_diffs=commit_diffs)


def _diff_touches_symbol(diff_body: str, symbol_name: str, leaf: str) -> bool:
    pattern = re.compile(
        rf"^[+-](?!\+\+ |-- ).*\b({re.escape(symbol_name)}|{re.escape(leaf)})\b",
        re.MULTILINE,
    )
    return pattern.search(diff_body) is not None


def _branches_containing(project_root: Path, sha: str) -> frozenset[str]:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(project_root), "branch", "--all",
                "--contains", sha, "--format=%(refname:short)",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return frozenset()
    branches: set[str] = set()
    for raw in result.stdout.splitlines():
        name = raw.strip()
        if not name:
            continue
        branches.add(name.removeprefix("origin/"))
    return frozenset(branches)


def symbol_allowed_branches(
    project_root: Path, history: _FileHistory | None, symbol_name: str
) -> frozenset[str] | None:
    """Branches reachable from a commit whose diff for this file mentions the symbol.

    Returns `None` when history is unavailable or no commit matched — this
    means "unknown", not "no branch is allowed"; `gold_for_symbol` treats
    `None` as "skip the branch filter" so a repo without deep git history
    still degrades to file+text grounding instead of producing empty gold.
    """
    if history is None:
        return None
    leaf = symbol_name.rsplit(".", 1)[-1]
    branches: set[str] = set()
    matched = False
    for sha, diff_body in history.commit_diffs.items():
        if _diff_touches_symbol(diff_body, symbol_name, leaf):
            matched = True
            branches |= _branches_containing(project_root, sha)
    return frozenset(branches) if matched else None


# ---- gold construction (pure given `con`) ----


def _index_edited_files(con: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """Group (exchange, text, branch) rows by the file an actual EDIT touched.

    Deliberately reads `code_touches` (file-level edit log: which exchange
    changed which file, no symbol resolution) — NOT `exchange_files`, which
    also records mere file *reads* (see `indexer._extract_tool_use_files`'s
    `Read` tool), nor `code_edges`/`code_symbols` (the symbol-level
    resolution under test).

    This distinction is load-bearing: `loci context <file>:<symbol>`
    promises git-blame semantics ("who edited this"), not "who ever looked
    at this". Gold built from `exchange_files` conflated the two — of 518
    gold hits measured against a real corpus, only 61 (12%) were actually
    edit-backed; the other 88% were conversations that merely read the file
    and happened to mention the symbol, which the lookup was never meant to
    surface. `code_touches.file_path` is already repo-relative (touch
    adapters normalize it at index time), so no path normalization is
    needed here.
    """
    rows = con.execute(
        """
        SELECT DISTINCT e.id, e.user_content, e.agent_content, e.git_branch,
               ct.file_path
        FROM code_touches ct
        JOIN exchanges e ON e.id = ct.exchange_id
        """
    ).fetchall()
    index: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        index.setdefault(row["file_path"], []).append(row)
    return index


def gold_for_symbol(
    file_rows: list[sqlite3.Row],
    symbol_name: str,
    allowed_branches: frozenset[str] | None,
) -> list[str]:
    """Exchange ids among `file_rows` (pre-filtered to one edited file) mentioning symbol_name verbatim.

    Reads only `code_touches` + `exchanges` verbatim text/git_branch —
    never `symbols`/`code_symbols`/`code_edges` (see module docstring).
    """
    leaf = symbol_name.rsplit(".", 1)[-1]
    pattern = re.compile(rf"\b({re.escape(symbol_name)}|{re.escape(leaf)})\b")
    gold: list[str] = []
    for row in file_rows:
        branch = row["git_branch"]
        if allowed_branches is not None and branch is not None and branch not in allowed_branches:
            continue
        text = f"{row['user_content'] or ''}\n{row['agent_content'] or ''}"
        if pattern.search(text):
            gold.append(row["id"])
    return gold


# ---- dataset assembly ----


def generate_symbol_recall_queries(
    con: sqlite3.Connection,
    project_root: Path,
    *,
    min_gold: int = 1,
    max_gold: int = 20,
    symbols: list[Symbol] | None = None,
    allowed_branches_fn: AllowedBranchesFn | None = None,
) -> list[Query]:
    """Build the symbol-recall dataset: one Query per symbol with 1..max_gold gold hits.

    `symbols` and `allowed_branches_fn` are injectable so tests can supply
    fake symbols and fake git info without a real git repo or tree-sitter run.
    """
    if symbols is None:
        symbols = enumerate_head_symbols(project_root)

    history_cache: dict[str, _FileHistory | None] = {}
    file_index = _index_edited_files(con)

    def _default_allowed_branches(
        root: Path, rel_path: str, name: str
    ) -> frozenset[str] | None:
        if rel_path not in history_cache:
            history_cache[rel_path] = _load_file_history(root, rel_path)
        return symbol_allowed_branches(root, history_cache[rel_path], name)

    resolve_branches = allowed_branches_fn or _default_allowed_branches

    queries: list[Query] = []
    seen: set[tuple[str, str]] = set()
    for sym in symbols:
        rel_path = normalize_repo_path(sym.file_path, str(project_root))
        if rel_path is None:
            continue
        key = (rel_path, sym.symbol_name)
        if key in seen:
            continue
        seen.add(key)

        allowed_branches = resolve_branches(project_root, rel_path, sym.symbol_name)
        gold = gold_for_symbol(
            file_index.get(rel_path, []), sym.symbol_name, allowed_branches
        )
        if min_gold <= len(gold) <= max_gold:
            queries.append(
                Query(
                    id=sha256(f"{DATASET_NAME}:{rel_path}:{sym.symbol_name}"),
                    kind="symbol",
                    value=f"{rel_path}::{sym.symbol_name}",
                    gold_exchange_ids=tuple(gold),
                )
            )
    return queries
