"""
HNSW ベクトル検索（Phase 1: verbatim embedding ベース）

Phase 2 でBM25(V) + HNSW(D) の CombMNZ 融合に移行する。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from logo.db import get_connection


@dataclass
class SearchResult:
    """検索結果の1件"""

    exchange_id: str
    user_content: str
    agent_content: str
    distance: float


def _serialize(vec: np.ndarray) -> bytes:
    """numpy float32 配列を sqlite-vec が受け取るバイナリに変換する"""
    arr = vec.astype(np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def search_hnsw(
    db_path: Path, query_vec: np.ndarray, limit: int = 5
) -> list[SearchResult]:
    """
    sqlite-vec HNSW で近傍 exchange を検索する。

    Args:
        db_path: DB パス
        query_vec: 384次元 float32 クエリベクトル
        limit: 返す件数の上限

    Returns:
        SearchResult のリスト（距離の近い順）
    """
    con = get_connection(db_path)
    blob = _serialize(query_vec)

    # sqlite-vec の knn クエリは LIMIT ではなく k = ? 制約が必要
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
