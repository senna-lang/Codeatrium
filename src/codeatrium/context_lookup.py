"""
`loci context` の U1/U2 解決ロジック（design §6.0・§6.1・§6.2）。

U1（ファイル内の関数・コンポーネント）: symbol(1.00) → file(0.45) → directory(0.25)
の順に試し、**最初にヒットした段だけ**を返す（下の段で埋め合わせない）。
U2（ファイルそのもの）: file(1.00) → directory(0.30) も同様。

semantic 段（0.10、design §6.2 の最終段）はここでは扱わない。embedding は重い
依存であり、`search_combined` を通じて呼び出し側（CLI 層）が担当する — このモジュールは
sqlite3.Connection だけで完結させる。

各ヒットには「周辺コンテキスト」を additive に添える（`ContextHit.context`）。
実測（本リポジトリの .codeatrium/memory.db）に基づく2レーン構成:
  - ply_adjacent: 同一会話内で ply_start 順に前後の exchange（hit の81%はこちらで解決）。
    ply_start には隙間があるため、値の距離ではなく列内の「順位」で前後K件を数える。
  - parent_session: hit がサブエージェント会話（parent_session_ref を持つ）に着地した
    場合、そのサブエージェント自身の前後は「次の機械的指示」でしかない（design §2.3）ため、
    親会話のうち同一ファイルを編集した exchange を代わりに返す（hit の19%が該当）。
どちらのレーンも anchor（hit 本体）を書き換えない。confidence を持たず、
`relation` で由来を明示した別枠の参考情報として返す。

DB を読むだけで書き込みはしない。接続のライフサイクルは呼び出し側が管理する。
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass, field

_TIER_SYMBOL_CONFIDENCE = 1.00
_TIER_FILE_CONFIDENCE_U1 = 0.45
_TIER_DIRECTORY_CONFIDENCE_U1 = 0.25
_TIER_FILE_CONFIDENCE_U2 = 1.00
_TIER_DIRECTORY_CONFIDENCE_U2 = 0.30

# ply_adjacent レーンの窓幅。前を厚く・後ろを薄くするのは、編集の動機になった議論は
# 直前に集中していることが多いという実測（本リポジトリの実例で確認済み）に基づく。
_PLY_WINDOW_BEFORE = 2
_PLY_WINDOW_AFTER = 1


@dataclass(frozen=True)
class ContextTarget:
    """`loci context <target>` の位置引数をパースした結果（design §6.1）"""

    file_path: str
    symbol_name: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ContextSnippet:
    """hit に添える周辺コンテキストの1件。confidence を持たない——anchor（hit 本体）とは
    別枠の参考情報であり、段カスケード（§6.2）の確信度の意味を薄めない。
    """

    relation: str  # "ply_adjacent"（同一会話の前後） | "parent_session"（親会話の同一ファイル編集）
    exchange_id: str
    ply: int
    exchange_core: str | None
    specific_context: str | None
    user_content: str
    agent_content: str
    verbatim_ref: str


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
    context: list[ContextSnippet] = field(default_factory=list)


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
        c.source_path, e.ply_start,
        e.conversation_id, c.parent_session_ref
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


def select_ply_window(
    ordered_exchange_ids: list[str], anchor_exchange_id: str, k_before: int, k_after: int
) -> list[str]:
    """会話内で ply_start 順に並べた exchange id 列から、anchor の前後 id を返す（純関数）。

    ply_start には隙間があり得る（1 exchange が複数行の生ログに対応するため）。
    値の距離ではなく、列内の「順位」で前後 K 件を数える——これを取り違えると
    ply の値でウィンドウを切ってしまい、実在する隣接 exchange を取りこぼす
    （実測で確認済みのバグ）。

    anchor が列に無ければ空を返す。列の端では、存在する分だけ返す。
    """
    try:
        idx = ordered_exchange_ids.index(anchor_exchange_id)
    except ValueError:
        return []
    before = ordered_exchange_ids[max(0, idx - k_before): idx]
    after = ordered_exchange_ids[idx + 1: idx + 1 + k_after]
    return before + after


def _conversation_exchanges_ordered(
    con: sqlite3.Connection, conversation_id: str
) -> list[sqlite3.Row]:
    """会話内の exchange を ply_start 順に、context 表示に要る列だけ取ってくる"""
    return con.execute(
        """
        SELECT e.id, e.ply_start, e.user_content, e.agent_content,
               p.exchange_core, p.specific_context
        FROM exchanges e
        LEFT JOIN palace_objects p ON p.exchange_id = e.id
        WHERE e.conversation_id = ?
        ORDER BY e.ply_start
        """,
        (conversation_id,),
    ).fetchall()


def _snippet_from_row(row: sqlite3.Row, relation: str, source_path: str) -> ContextSnippet:
    return ContextSnippet(
        relation=relation,
        exchange_id=row["id"],
        ply=row["ply_start"],
        exchange_core=row["exchange_core"],
        specific_context=row["specific_context"],
        user_content=row["user_content"],
        agent_content=row["agent_content"],
        verbatim_ref=f"{source_path}:ply={row['ply_start']}",
    )


def ply_adjacent_context(
    con: sqlite3.Connection, conversation_id: str, anchor_exchange_id: str, source_path: str
) -> list[ContextSnippet]:
    """同一会話内の ply 隣接コンテキストを返す。`loci context` hit の主レーン（design:
    hit の81%はここで解決）であると同時に、`loci show <exchange_id>` からも呼べる公開関数
    ——任意の exchange を起点に前後へ辿る（design: 案2撤回に伴う代替、show の追加パラメータ
    無しで既存出力に additive で乗せる）。
    """
    rows = _conversation_exchanges_ordered(con, conversation_id)
    by_id = {r["id"]: r for r in rows}
    neighbor_ids = select_ply_window(
        [r["id"] for r in rows], anchor_exchange_id, _PLY_WINDOW_BEFORE, _PLY_WINDOW_AFTER
    )
    return [_snippet_from_row(by_id[nid], "ply_adjacent", source_path) for nid in neighbor_ids]


def _parent_session_context(
    con: sqlite3.Connection, parent_source_path: str, file_path: str
) -> list[ContextSnippet]:
    """副レーン（design: hit の19%、サブエージェント発の機械的指示ヒット）。

    サブエージェント自身の前後は判断理由を持たない（design §2.3）ため、代わりに
    親会話のうち同一ファイルを編集した exchange を返す。見つからなければ空を返す
    ——適当な代替を出さない（design §6.2 の方針）。
    """
    parent = con.execute(
        "SELECT id FROM conversations WHERE source_path = ?", (parent_source_path,)
    ).fetchone()
    if parent is None:
        return []
    rows = con.execute(
        """
        SELECT e.id, e.ply_start, e.user_content, e.agent_content,
               p.exchange_core, p.specific_context
        FROM code_edges ce
        JOIN exchanges e ON e.id = ce.exchange_id
        LEFT JOIN palace_objects p ON p.exchange_id = e.id
        WHERE e.conversation_id = ? AND ce.file_path = ?
        ORDER BY e.ply_start
        """,
        (parent["id"], file_path),
    ).fetchall()
    return [_snippet_from_row(r, "parent_session", parent_source_path) for r in rows]


def _build_context(con: sqlite3.Connection, row: sqlite3.Row) -> list[ContextSnippet]:
    """hit の周辺コンテキストを2レーンから組み立てる（design: advisory反映、anchor は
    書き換えず additive に添えるだけ）。parent_session_ref があるサブエージェント会話
    ならそちらを優先し、無ければ同一会話内の ply 隣接を返す。
    """
    parent_ref = row["parent_session_ref"]
    if parent_ref:
        return _parent_session_context(con, parent_ref, row["file_path"])
    return ply_adjacent_context(con, row["conversation_id"], row["exchange_id"], row["source_path"])


def _row_to_hit(con: sqlite3.Connection, row: sqlite3.Row, match_kind: str, confidence: float) -> ContextHit:
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
        context=_build_context(con, row),
    )


def _file_rows(
    con: sqlite3.Connection, file_path: str, alias_paths: tuple[str, ...] = ()
) -> list[sqlite3.Row]:
    paths = (file_path, *alias_paths)
    placeholders = ",".join("?" for _ in paths)
    return _dedup_by_exchange(
        _query_rows(con, f"ce.file_path IN ({placeholders})", paths)
    )


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
    con: sqlite3.Connection,
    file_path: str,
    symbol_name: str,
    limit: int,
    alias_paths: tuple[str, ...] = (),
) -> list[ContextHit]:
    """U1: symbol(1.00) → file(0.45) → directory(0.25) の順に試す（design §6.2）。

    最初にヒットした段だけを返す。`limit` は最終的な件数の上限であり、
    ヒットした段の件数がそれに満たなくても下の段から埋め合わせない
    （埋め合わせると確信度の意味が壊れる）。semantic 段は呼び出し側の責務。

    `alias_paths` は `file_path` の旧パス（design §8.2、ファイル改名）。
    同じファイルの別名として symbol/file 段のクエリに加えるだけで、確信度や
    段構成そのものは変えない――改名前の会話も「同じファイル」として symbol
    段でヒットする（別の下位ティアを新設しない）。
    """
    paths = (file_path, *alias_paths)
    placeholders = ",".join("?" for _ in paths)
    rows = _dedup_by_exchange(
        _query_rows(
            con,
            f"ce.file_path IN ({placeholders}) AND cs.symbol_name = ?",
            (*paths, symbol_name),
        )
    )
    if rows:
        return [_row_to_hit(con, r, "symbol", _TIER_SYMBOL_CONFIDENCE) for r in rows[:limit]]

    rows = _file_rows(con, file_path, alias_paths)
    if rows:
        return [_row_to_hit(con, r, "file", _TIER_FILE_CONFIDENCE_U1) for r in rows[:limit]]

    rows = _directory_rows(con, file_path)
    if rows:
        return [
            _row_to_hit(con, r, "directory", _TIER_DIRECTORY_CONFIDENCE_U1) for r in rows[:limit]
        ]

    return []


def resolve_u2(
    con: sqlite3.Connection,
    file_path: str,
    limit: int,
    alias_paths: tuple[str, ...] = (),
) -> list[ContextHit]:
    """U2: file(1.00) → directory(0.30) の順に試す（design §6.2）。

    file 段は「このファイルについて何が決まっているか」を知りたい用途なので、
    一番良い1件だけでなく、シンボルごとにまとめて複数返す。semantic 段は
    呼び出し側の責務。`alias_paths` は resolve_u1 と同じ意味（design §8.2）。
    """
    rows = _file_rows(con, file_path, alias_paths)
    if rows:
        return [_row_to_hit(con, r, "file", _TIER_FILE_CONFIDENCE_U2) for r in rows[:limit]]

    rows = _directory_rows(con, file_path)
    if rows:
        return [
            _row_to_hit(con, r, "directory", _TIER_DIRECTORY_CONFIDENCE_U2) for r in rows[:limit]
        ]

    return []
