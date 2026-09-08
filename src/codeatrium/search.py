"""検索モジュール — BM25(V) + HNSW(D) RRF 融合
  採用根拠（arXiv:2603.13017）:
    - 107構成比較で Cross BM25(V)+HNSW(D) が全クエリタイプで最良（MRR 0.759）
    - BM25 単独は全構成で有意に劣化 → クエリタイプで切り替えない
    - 融合は RRF (Reciprocal Rank Fusion): score = Σ 1/(k+rank)
      CombMNZ の hit_count 乗数問題を回避・スコア正規化不要
  HNSW(verbatim) は含めない（verbatim 長文は embedding 品質低・論文評価で有意改善なし）

検索結果には SPEC 準拠で verbatim_ref / rooms / symbols を付加する。
  - verbatim_ref: "{source_path}:ply={ply_start}"
  - rooms: palace_objects に紐づく room_assignments
  - symbols: palace_objects に紐づく tree-sitter 解決済みシンボル
"""

from __future__ import annotations

import sqlite3
import struct
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any

import numpy as np

from codeatrium.db import get_connection
from codeatrium.models import (
    BM25Result,
    FusedResult,
    HNSWPalaceResult,
)
from codeatrium.utils import escape_like

# ---- 内部ヘルパー ----


def _serialize(vec: np.ndarray) -> bytes:
    arr = vec.astype(np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _enrich_results(con: sqlite3.Connection, results: list[FusedResult]) -> None:
    """FusedResult リストに verbatim_ref / rooms / symbols を付加する（in-place）。"""
    if not results:
        return

    exchange_ids = [r.exchange_id for r in results]
    placeholders = ",".join("?" * len(exchange_ids))

    ref_rows = con.execute(
        f"""
        SELECT e.id, c.source_path, e.ply_start, e.git_branch
        FROM exchanges e
        JOIN conversations c ON c.id = e.conversation_id
        WHERE e.id IN ({placeholders})
        """,
        exchange_ids,
    ).fetchall()
    ref_map = {r["id"]: f"{r['source_path']}:ply={r['ply_start']}" for r in ref_rows}
    branch_map = {r["id"]: r["git_branch"] for r in ref_rows}

    room_rows = con.execute(
        f"""
        SELECT p.exchange_id, r.room_type, r.room_key, r.room_label, r.relevance
        FROM palace_objects p
        JOIN rooms r ON r.palace_object_id = p.id
        WHERE p.exchange_id IN ({placeholders})
        ORDER BY r.relevance DESC
        """,
        exchange_ids,
    ).fetchall()
    rooms_map: dict[str, list[dict[str, Any]]] = {}
    for r in room_rows:
        rooms_map.setdefault(r["exchange_id"], []).append(
            {
                "room_type": r["room_type"],
                "room_key": r["room_key"],
                "room_label": r["room_label"],
                "relevance": r["relevance"],
            }
        )

    sym_rows = con.execute(
        f"""
        SELECT p.exchange_id, s.symbol_name, s.file_path, s.line, s.signature
        FROM palace_objects p
        JOIN symbols s ON s.palace_object_id = p.id
        WHERE p.exchange_id IN ({placeholders})
        """,
        exchange_ids,
    ).fetchall()
    symbols_map: dict[str, list[dict[str, Any]]] = {}
    for s in sym_rows:
        symbols_map.setdefault(s["exchange_id"], []).append(
            {
                "name": s["symbol_name"],
                "file": s["file_path"],
                "line": s["line"],
                "signature": s["signature"],
            }
        )

    for r in results:
        r.verbatim_ref = ref_map.get(r.exchange_id)
        r.rooms = rooms_map.get(r.exchange_id, [])
        r.symbols = symbols_map.get(r.exchange_id, [])
        r.git_branch = branch_map.get(r.exchange_id)


# ---- BM25 verbatim ----


def _fts5_query(text: str) -> str:
    """クエリを FTS5 OR 形式に変換する。"""
    tokens = text.split()
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens if t]
    return " OR ".join(escaped) if escaped else text


def search_bm25(
    db_path: Path,
    query_text: str,
    limit: int = 10,
    min_exchanges: int = 2,
    branch: str | None = None,
    con: sqlite3.Connection | None = None,
) -> list[BM25Result]:
    """FTS5 BM25 で exchanges_fts を検索する。

    `con` を渡すと呼び出し側の接続をそのまま使い、クローズは呼び出し側の責務になる
    （`search_combined` が bm25/hnsw/enrich で1接続を共有するため、issue #25）。
    省略時は従来どおり自前で接続を開き、返す前に閉じる。
    """
    fts_query = _fts5_query(query_text)
    branch_clause = "AND e.git_branch LIKE ? ESCAPE '\\'" if branch is not None else ''
    branch_params: list = [f'%{escape_like(branch)}%'] if branch is not None else []
    ctx = nullcontext(con) if con is not None else closing(get_connection(db_path))
    with ctx as c:
        try:
            rows = c.execute(
                f"""
                SELECT
                    e.id          AS exchange_id,
                    e.user_content,
                    e.agent_content,
                    -bm25(exchanges_fts) AS score
                FROM exchanges_fts
                JOIN exchanges e ON e.rowid = exchanges_fts.rowid
                WHERE exchanges_fts MATCH ?
                  AND (SELECT COUNT(*) FROM exchanges e2
                       WHERE e2.conversation_id = e.conversation_id) >= ?
                {branch_clause}
                ORDER BY score DESC
                LIMIT ?
                """,
                (fts_query, min_exchanges, *branch_params, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return [
        BM25Result(
            exchange_id=row["exchange_id"],
            user_content=row["user_content"],
            agent_content=row["agent_content"],
            bm25_score=row["score"],
        )
        for row in rows
    ]


# ---- HNSW distilled ----


_KNN_INITIAL_CANDIDATE_FACTOR = 5
_KNN_CANDIDATE_GROWTH_FACTOR = 2
_KNN_MAX_CANDIDATE_K = 2000
"""候補生成側の k を適応的に広げるためのパラメータ。

vec0 の KNN は min_exchanges/branch フィルタより先に k 件で打ち切られる。
k を固定倍率（例: limit*5）に決め打ちすると、フィルタの選択率がその倍率を
超えて厳しい場合（例: 上位 5*limit 件が軒並みフィルタ対象外）に、閾値が
変わっただけで同じ recall 消失が再発する（issue #18 レビュー指摘）。
そこで k は `limit * _KNN_INITIAL_CANDIDATE_FACTOR` から始め、フィルタ後の
件数が limit に満たず、かつ vec_palace 全体をまだ使い切っていなければ
`_KNN_CANDIDATE_GROWTH_FACTOR` 倍して再試行する。無制限に広げるとフィルタが
極端に厳しい場合に ANN 走査コストが際限なく増えるため、
`_KNN_MAX_CANDIDATE_K` を最終的なハードキャップとする（この上限に達しても
limit 件に満たない場合は、その時点までに見つかった最良の候補を返す）。
"""


def search_hnsw_palace(
    db_path: Path,
    query_vec: np.ndarray,
    limit: int = 10,
    min_exchanges: int = 2,
    branch: str | None = None,
    con: sqlite3.Connection | None = None,
) -> list[HNSWPalaceResult]:
    """sqlite-vec HNSW で vec_palace を検索する（distilled embedding）。

    `con` の共有規約は `search_bm25` と同じ（issue #25）。
    """
    branch_clause = "AND e.git_branch LIKE ? ESCAPE '\\'" if branch is not None else ''
    branch_params: list = [f'%{escape_like(branch)}%'] if branch is not None else []

    ctx = nullcontext(con) if con is not None else closing(get_connection(db_path))
    with ctx as c:
        blob = _serialize(query_vec)
        rows: list[sqlite3.Row] = []
        try:
            total_row = c.execute("SELECT COUNT(*) AS n FROM vec_palace").fetchone()
            total_candidates = total_row["n"] if total_row is not None else 0

            candidate_k = min(limit * _KNN_INITIAL_CANDIDATE_FACTOR, _KNN_MAX_CANDIDATE_K)
            while True:
                rows = c.execute(
                    f"""
                    SELECT
                        p.exchange_id,
                        e.user_content,
                        e.agent_content,
                        p.exchange_core,
                        p.specific_context,
                        v.distance
                    FROM (
                        SELECT palace_id, distance
                        FROM vec_palace
                        WHERE embedding MATCH ?
                        AND k = ?
                    ) v
                    JOIN palace_objects p ON p.id = v.palace_id
                    JOIN exchanges e ON e.id = p.exchange_id
                    WHERE (SELECT COUNT(*) FROM exchanges e2
                           WHERE e2.conversation_id = e.conversation_id) >= ?
                    {branch_clause}
                    ORDER BY v.distance
                    LIMIT ?
                    """,
                    (blob, candidate_k, min_exchanges, *branch_params, limit),
                ).fetchall()

                if len(rows) >= limit:
                    break
                exhausted_index = candidate_k >= total_candidates
                at_hard_cap = candidate_k >= _KNN_MAX_CANDIDATE_K
                if exhausted_index or at_hard_cap:
                    break
                candidate_k = min(candidate_k * _KNN_CANDIDATE_GROWTH_FACTOR, _KNN_MAX_CANDIDATE_K)
        except sqlite3.OperationalError:
            rows = []

    return [
        HNSWPalaceResult(
            exchange_id=row["exchange_id"],
            user_content=row["user_content"],
            agent_content=row["agent_content"],
            exchange_core=row["exchange_core"],
            specific_context=row["specific_context"],
            distance=row["distance"],
        )
        for row in rows
    ]


# ---- RRF 融合 ----


def rrf(
    bm25_results: list[BM25Result],
    hnsw_results: list[HNSWPalaceResult],
    limit: int = 5,
    k: int = 60,
) -> list[FusedResult]:
    """BM25(V) と HNSW(D) の結果を RRF (Reciprocal Rank Fusion) で融合する。"""
    if not bm25_results and not hnsw_results:
        return []

    scores: dict[str, float] = {}
    for rank, r in enumerate(bm25_results, 1):
        scores[r.exchange_id] = scores.get(r.exchange_id, 0.0) + 1.0 / (k + rank)
    for rank, r in enumerate(hnsw_results, 1):
        scores[r.exchange_id] = scores.get(r.exchange_id, 0.0) + 1.0 / (k + rank)

    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]

    bm25_map = {r.exchange_id: r for r in bm25_results}
    hnsw_map = {r.exchange_id: r for r in hnsw_results}

    results: list[FusedResult] = []
    for eid in sorted_ids:
        if eid in bm25_map:
            r_base = bm25_map[eid]
        else:
            r_base = hnsw_map[eid]
        palace_r = hnsw_map.get(eid)
        results.append(
            FusedResult(
                exchange_id=eid,
                user_content=r_base.user_content,
                agent_content=r_base.agent_content,
                score=scores[eid],
                exchange_core=palace_r.exchange_core if palace_r else None,
                specific_context=palace_r.specific_context if palace_r else None,
            )
        )

    return results


# ---- メイン検索 ----


def search_combined(
    db_path: Path,
    query_text: str,
    query_vec: np.ndarray,
    limit: int = 5,
    min_exchanges: int = 2,
    branch: str | None = None,
) -> list[FusedResult]:
    """BM25(V) + HNSW(D) の RRF 融合検索。

    bm25/hnsw/enrich の3クエリ群で1つの sqlite 接続を共有する。接続ごとに
    sqlite-vec 拡張ロード + WAL/busy_timeout PRAGMA が再実行されるコストを
    1検索あたり3回から1回に減らす（issue #25）。
    """
    with closing(get_connection(db_path)) as con:
        bm25_results = search_bm25(
            db_path, query_text, limit=limit * 2, min_exchanges=min_exchanges, branch=branch, con=con
        )
        hnsw_results = search_hnsw_palace(
            db_path, query_vec, limit=limit * 2, min_exchanges=min_exchanges, branch=branch, con=con
        )
        fused = rrf(bm25_results, hnsw_results, limit=limit)

        if fused:
            _enrich_results(con, fused)

    return fused
