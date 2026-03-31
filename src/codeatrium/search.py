"""
検索モジュール

Phase 1: search_hnsw    — verbatim HNSW（後方互換で維持）
Phase 2: search_combined — BM25(V) + HNSW(D) RRF 融合
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from codeatrium.db import get_connection

# ---- データクラス ----


@dataclass
class SearchResult:
    """Phase 1 HNSW 検索結果"""

    exchange_id: str
    user_content: str
    agent_content: str
    distance: float


@dataclass
class BM25Result:
    """BM25 verbatim 検索結果"""

    exchange_id: str
    user_content: str
    agent_content: str
    bm25_score: float  # 正値（高いほど良い）


@dataclass
class HNSWPalaceResult:
    """HNSW distilled 検索結果"""

    exchange_id: str
    user_content: str
    agent_content: str
    exchange_core: str
    specific_context: str
    distance: float


@dataclass
class FusedResult:
    """RRF 融合検索結果（SPEC 準拠の出力フォーマット）"""

    exchange_id: str
    user_content: str
    agent_content: str
    score: float
    exchange_core: str | None = None
    specific_context: str | None = None
    verbatim_ref: str | None = None
    rooms: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)


# ---- 内部ヘルパー ----


def _serialize(vec: np.ndarray) -> bytes:
    arr = vec.astype(np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _enrich_results(con: sqlite3.Connection, results: list[FusedResult]) -> None:
    """
    FusedResult リストに verbatim_ref / rooms / symbols を付加する（in-place）。

    SPEC の検索出力フォーマット:
      { exchange_core, specific_context, verbatim_ref, rooms: [...], symbols: [...] }
    """
    if not results:
        return

    exchange_ids = [r.exchange_id for r in results]
    placeholders = ",".join("?" * len(exchange_ids))

    # verbatim_ref: source_path + ply_start
    ref_rows = con.execute(
        f"""
        SELECT e.id, c.source_path, e.ply_start
        FROM exchanges e
        JOIN conversations c ON c.id = e.conversation_id
        WHERE e.id IN ({placeholders})
        """,
        exchange_ids,
    ).fetchall()
    ref_map = {r["id"]: f"{r['source_path']}:ply={r['ply_start']}" for r in ref_rows}

    # rooms
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

    # symbols
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


# ---- Phase 1: HNSW verbatim（後方互換） ----


def search_hnsw(
    db_path: Path, query_vec: np.ndarray, limit: int = 5
) -> list[SearchResult]:
    """sqlite-vec HNSW で近傍 exchange を検索する（verbatim embedding）"""
    con = get_connection(db_path)
    blob = _serialize(query_vec)

    rows = con.execute(
        """
        SELECT
            v.exchange_id,
            e.user_content,
            e.agent_content,
            v.distance
        FROM (
            SELECT exchange_id, distance
            FROM vec_exchanges
            WHERE embedding MATCH ?
            AND k = ?
        ) v
        JOIN exchanges e ON e.id = v.exchange_id
        ORDER BY v.distance
        """,
        (blob, limit),
    ).fetchall()

    con.close()
    return [
        SearchResult(
            exchange_id=row["exchange_id"],
            user_content=row["user_content"],
            agent_content=row["agent_content"],
            distance=row["distance"],
        )
        for row in rows
    ]


# ---- Phase 2: BM25 verbatim ----


def _fts5_query(text: str) -> str:
    """クエリを FTS5 OR 形式に変換する。

    FTS5 のデフォルトは AND 結合で、全トークンが揃わないとヒットしない。
    スペース区切りで分割して OR 結合に変換することで再現率を上げる。
    ダブルクォートで囲んでフレーズ検索特殊文字をエスケープする。
    """
    tokens = text.split()
    escaped = ['"' + t.replace('"', '""') + '"' for t in tokens if t]
    return " OR ".join(escaped) if escaped else text


def search_bm25(db_path: Path, query_text: str, limit: int = 10) -> list[BM25Result]:
    """FTS5 BM25 で exchanges_fts を検索する"""
    con = get_connection(db_path)
    fts_query = _fts5_query(query_text)
    try:
        rows = con.execute(
            """
            SELECT
                e.id          AS exchange_id,
                e.user_content,
                e.agent_content,
                -bm25(exchanges_fts) AS score
            FROM exchanges_fts
            JOIN exchanges e ON e.rowid = exchanges_fts.rowid
            WHERE exchanges_fts MATCH ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return [
        BM25Result(
            exchange_id=row["exchange_id"],
            user_content=row["user_content"],
            agent_content=row["agent_content"],
            bm25_score=row["score"],
        )
        for row in rows
    ]


# ---- Phase 2: HNSW distilled ----


def search_hnsw_palace(
    db_path: Path, query_vec: np.ndarray, limit: int = 10
) -> list[HNSWPalaceResult]:
    """sqlite-vec HNSW で vec_palace を検索する（distilled embedding）"""
    con = get_connection(db_path)
    blob = _serialize(query_vec)

    try:
        rows = con.execute(
            """
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
            ORDER BY v.distance
            """,
            (blob, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    con.close()
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


# ---- Phase 2: RRF 融合 ----


def rrf(
    bm25_results: list[BM25Result],
    hnsw_results: list[HNSWPalaceResult],
    limit: int = 5,
    k: int = 60,
) -> list[FusedResult]:
    """
    BM25(V) と HNSW(D) の結果を RRF (Reciprocal Rank Fusion) で融合する。

    RRF: score(d) = Σ 1 / (k + rank_i(d))
    k=60 は標準値（Cormack et al., 2009）。
    スコアの絶対値に依存せず順位だけで融合するため正規化不要。

    注意: verbatim_ref / rooms / symbols は search_combined で付加される。
    rrf() 単体では rooms=[] / symbols=[] のまま返す。
    """
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


# ---- Phase 2: メイン検索 ----


def search_combined(
    db_path: Path,
    query_text: str,
    query_vec: np.ndarray,
    limit: int = 5,
) -> list[FusedResult]:
    """
    BM25(V) + HNSW(D) の RRF 融合検索。

    palace objects がない場合は BM25 のみで結果を返す。
    結果には verbatim_ref / rooms / symbols を付加する（SPEC 準拠）。
    """
    bm25_results = search_bm25(db_path, query_text, limit=limit * 2)
    hnsw_results = search_hnsw_palace(db_path, query_vec, limit=limit * 2)
    fused = rrf(bm25_results, hnsw_results, limit=limit)

    if fused:
        con = get_connection(db_path)
        _enrich_results(con, fused)
        con.close()

    return fused
