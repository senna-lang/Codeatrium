"""
検索モジュール

Phase 1: search_hnsw    — verbatim HNSW（後方互換で維持）
Phase 2: search_combined — BM25(V) + HNSW(D) CombMNZ 融合
  論文ベスト構成: Cross BM25(V)+HNSW(D) / CombMNZ → MRR 0.759
  HNSW(verbatim) は論文ベスト構成に含まれないため search_combined には使わない
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from logo.db import get_connection


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
class CombMNZResult:
    """CombMNZ 融合検索結果"""

    exchange_id: str
    user_content: str
    agent_content: str
    score: float
    exchange_core: str | None = None
    specific_context: str | None = None


# ---- 内部ヘルパー ----


def _serialize(vec: np.ndarray) -> bytes:
    arr = vec.astype(np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _normalize(scores: dict[str, float]) -> dict[str, float]:
    """min-max 正規化して [0, 1] に変換する"""
    if not scores:
        return {}
    vals = list(scores.values())
    min_v, max_v = min(vals), max(vals)
    if min_v == max_v:
        return {k: 1.0 for k in scores}
    return {k: (v - min_v) / (max_v - min_v) for k, v in scores.items()}


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


def search_bm25(
    db_path: Path, query_text: str, limit: int = 10
) -> list[BM25Result]:
    """FTS5 BM25 で exchanges_fts を検索する"""
    con = get_connection(db_path)
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
            (query_text, limit),
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


# ---- Phase 2: CombMNZ 融合 ----


def combmnz(
    bm25_results: list[BM25Result],
    hnsw_results: list[HNSWPalaceResult],
    limit: int = 5,
) -> list[CombMNZResult]:
    """
    BM25(V) と HNSW(D) の結果を CombMNZ で融合する。

    CombMNZ: score = Σ(normalized_scores) × (ヒットしたリスト数)
    論文ベスト構成: Cross BM25(V)+HNSW(D) / CombMNZ → MRR 0.759
    """
    if not bm25_results and not hnsw_results:
        return []

    bm25_raw = {r.exchange_id: r.bm25_score for r in bm25_results}
    hnsw_raw = {r.exchange_id: 1.0 / (1.0 + r.distance) for r in hnsw_results}

    norm_bm25 = _normalize(bm25_raw)
    norm_hnsw = _normalize(hnsw_raw)

    all_ids = set(norm_bm25) | set(norm_hnsw)

    combmnz_scores: dict[str, float] = {}
    for eid in all_ids:
        hit_count = (1 if eid in norm_bm25 else 0) + (1 if eid in norm_hnsw else 0)
        combined = norm_bm25.get(eid, 0.0) + norm_hnsw.get(eid, 0.0)
        combmnz_scores[eid] = combined * hit_count

    sorted_ids = sorted(combmnz_scores, key=lambda k: combmnz_scores[k], reverse=True)[
        :limit
    ]

    bm25_map = {r.exchange_id: r for r in bm25_results}
    hnsw_map = {r.exchange_id: r for r in hnsw_results}

    results: list[CombMNZResult] = []
    for eid in sorted_ids:
        if eid in bm25_map:
            r = bm25_map[eid]
            user_content, agent_content = r.user_content, r.agent_content
        else:
            r = hnsw_map[eid]
            user_content, agent_content = r.user_content, r.agent_content

        palace_r = hnsw_map.get(eid)
        results.append(
            CombMNZResult(
                exchange_id=eid,
                user_content=user_content,
                agent_content=agent_content,
                score=combmnz_scores[eid],
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
) -> list[CombMNZResult]:
    """
    BM25(V) + HNSW(D) の CombMNZ 融合検索。

    palace objects がない場合は BM25 のみで結果を返す。
    """
    bm25_results = search_bm25(db_path, query_text, limit=limit * 2)
    hnsw_results = search_hnsw_palace(db_path, query_vec, limit=limit * 2)
    return combmnz(bm25_results, hnsw_results, limit=limit)
