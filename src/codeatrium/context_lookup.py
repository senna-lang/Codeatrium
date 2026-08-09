"""
`loci context` の U1/U2 解決ロジック（design §6.0・§6.1・§6.2）。

U1（ファイル内の関数・コンポーネント）: symbol(1.00) → file(0.45) → directory(0.25)
の順に試し、**最初にヒットした段だけ**を返す（下の段で埋め合わせない）。
U2（ファイルそのもの）: file(1.00) → directory(0.30) も同様。

semantic 段（0.10、design §6.2 の最終段）はここでは扱わない。embedding は重い
依存であり、`search_combined` を通じて呼び出し側（CLI 層）が担当する — このモジュールは
sqlite3.Connection だけで完結させる。

DB を読むだけで書き込みはしない。接続のライフサイクルは呼び出し側が管理する。
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass

_TIER_SYMBOL_CONFIDENCE = 1.00
_TIER_FILE_CONFIDENCE_U1 = 0.45
_TIER_DIRECTORY_CONFIDENCE_U1 = 0.25
_TIER_FILE_CONFIDENCE_U2 = 1.00
_TIER_DIRECTORY_CONFIDENCE_U2 = 0.30


@dataclass(frozen=True)
class ContextTarget:
    """`loci context <target>` の位置引数をパースした結果（design §6.1）"""

    file_path: str
    symbol_name: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ContextHit:
    """U1/U2 の1件のヒット。`match_kind`/`confidence` がどの段で見つかったかを表す"""

    match_kind: str  # 'symbol' | 'file' | 'directory' | 'semantic'
    confidence: float
    exchange_id: str
    file_path: str
    symbol_name: str | None
    exchange_core: str | None
    specific_context: str | None
    verbatim_ref: str | None
    git_branch: str | None
    user_content: str | None = None
    agent_content: str | None = None


def parse_context_target(target: str) -> ContextTarget:
    """`<file>[:<symbol-or-line>]` を解釈する（design §6.1）。

    末尾が数値なら行番号、それ以外はシンボル名として扱う。ただしコロンの後ろに
    "/" が含まれる場合は、そのコロンはファイルパスの一部とみなし全体をファイルパス
    として扱う（POSIX ではファイル名にコロンを含められるため、`src/a:b/foo.py` を
    誤ってファイル `src/a` ・シンボル `b/foo.py` と解釈しないようにする）。
    """
    head, sep, tail = target.rpartition(":")
    if not sep or "/" in tail:
        return ContextTarget(file_path=target)
    if tail.isdigit():
        return ContextTarget(file_path=head, line=int(tail))
    return ContextTarget(file_path=head, symbol_name=tail)


def pick_enclosing_symbol_name(
    line: int, symbols: list[tuple[str, int, int]]
) -> str | None:
    """`line` を含むシンボルの名前を返す（design §6.1「行を指定すると、その行を
    含む関数に変換して U1 として扱う」）。IDE の選択範囲を U1 へ落とし込む変換。

    symbols は (symbol_name, line, end_line) のリスト。複数の候補区間が重なる
    場合は最初に見つかったものを返す（現状 tree-sitter はネスト関数を追跡しない
    ため、実際にはほぼ起こらない）。含む区間が無ければ None（呼び出し側は
    U1 ではなく U2 として扱う）。
    """
    for name, start, end in symbols:
        if start <= line <= end:
            return name
    return None


# ---- DB 問い合わせ ----

_HIT_QUERY = """
    SELECT
        ce.exchange_id, ce.file_path, cs.symbol_name,
        e.git_branch, e.user_content, e.agent_content,
        p.exchange_core, p.specific_context,
        c.source_path, e.ply_start
    FROM code_edges ce
    LEFT JOIN code_symbols cs ON cs.id = ce.symbol_id
    JOIN exchanges e ON e.id = ce.exchange_id
    JOIN conversations c ON c.id = e.conversation_id
    LEFT JOIN palace_objects p ON p.exchange_id = e.id
    WHERE {where}
    ORDER BY ce.ts DESC
"""


def _query_rows(
    con: sqlite3.Connection, where: str, params: tuple
) -> list[sqlite3.Row]:
    return con.execute(_HIT_QUERY.format(where=where), params).fetchall()


def _dedup_by_exchange(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """同じ exchange から複数の code_edges がヒットしても1回だけ返す"""
    seen: set[str] = set()
    out: list[sqlite3.Row] = []
    for row in rows:
        if row["exchange_id"] in seen:
            continue
        seen.add(row["exchange_id"])
        out.append(row)
    return out


def _row_to_hit(row: sqlite3.Row, match_kind: str, confidence: float) -> ContextHit:
    return ContextHit(
        match_kind=match_kind,
        confidence=confidence,
        exchange_id=row["exchange_id"],
        file_path=row["file_path"],
        symbol_name=row["symbol_name"],
        exchange_core=row["exchange_core"],
        specific_context=row["specific_context"],
        verbatim_ref=f"{row['source_path']}:ply={row['ply_start']}",
        git_branch=row["git_branch"],
        user_content=row["user_content"],
        agent_content=row["agent_content"],
    )


def _file_rows(con: sqlite3.Connection, file_path: str) -> list[sqlite3.Row]:
    return _dedup_by_exchange(_query_rows(con, "ce.file_path = ?", (file_path,)))


def _directory_rows(con: sqlite3.Connection, file_path: str) -> list[sqlite3.Row]:
    """`file_path` と同じディレクトリ（直下のみ、再帰しない）の code_edges を返す"""
    directory = posixpath.dirname(file_path)
    if directory:
        candidates = _query_rows(con, "ce.file_path LIKE ?", (f"{directory}/%",))
    else:
        candidates = _query_rows(con, "ce.file_path NOT LIKE '%/%'", ())
    same_dir = [r for r in candidates if posixpath.dirname(r["file_path"]) == directory]
    return _dedup_by_exchange(same_dir)


def resolve_u1(
    con: sqlite3.Connection, file_path: str, symbol_name: str, limit: int
) -> list[ContextHit]:
    """U1: symbol(1.00) → file(0.45) → directory(0.25) の順に試す（design §6.2）。

    最初にヒットした段だけを返す。`limit` は最終的な件数の上限であり、
    ヒットした段の件数がそれに満たなくても下の段から埋め合わせない
    （埋め合わせると確信度の意味が壊れる）。semantic 段は呼び出し側の責務。
    """
    rows = _dedup_by_exchange(
        _query_rows(
            con, "ce.file_path = ? AND cs.symbol_name = ?", (file_path, symbol_name)
        )
    )
    if rows:
        return [_row_to_hit(r, "symbol", _TIER_SYMBOL_CONFIDENCE) for r in rows[:limit]]

    rows = _file_rows(con, file_path)
    if rows:
        return [_row_to_hit(r, "file", _TIER_FILE_CONFIDENCE_U1) for r in rows[:limit]]

    rows = _directory_rows(con, file_path)
    if rows:
        return [
            _row_to_hit(r, "directory", _TIER_DIRECTORY_CONFIDENCE_U1) for r in rows[:limit]
        ]

    return []


def resolve_u2(con: sqlite3.Connection, file_path: str, limit: int) -> list[ContextHit]:
    """U2: file(1.00) → directory(0.30) の順に試す（design §6.2）。

    file 段は「このファイルについて何が決まっているか」を知りたい用途なので、
    一番良い1件だけでなく、シンボルごとにまとめて複数返す。semantic 段は
    呼び出し側の責務。
    """
    rows = _file_rows(con, file_path)
    if rows:
        return [_row_to_hit(r, "file", _TIER_FILE_CONFIDENCE_U2) for r in rows[:limit]]

    rows = _directory_rows(con, file_path)
    if rows:
        return [
            _row_to_hit(r, "directory", _TIER_DIRECTORY_CONFIDENCE_U2) for r in rows[:limit]
        ]

    return []
