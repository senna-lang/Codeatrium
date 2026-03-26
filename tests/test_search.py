"""
HNSW 検索のテスト
embedding は固定ベクトルで代替してモデルロードを避ける
"""

import struct
from pathlib import Path

import numpy as np

from logo.db import get_connection, init_db
from logo.search import SearchResult, search_hnsw


def insert_exchange_with_vec(
    con, exchange_id: str, user_content: str, agent_content: str, vec: np.ndarray
) -> None:
    """テスト用: exchange と vec を直接挿入する"""
    conv_id = "test-conv"
    con.execute(
        """INSERT OR IGNORE INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (exchange_id, conv_id, 0, 1, user_content, agent_content),
    )
    blob = struct.pack(f"{len(vec)}f", *vec.tolist())
    con.execute(
        "INSERT OR IGNORE INTO vec_exchanges (exchange_id, embedding) VALUES (?, ?)",
        (exchange_id, blob),
    )
    con.commit()


def test_search_hnsw_returns_results(tmp_path: Path) -> None:
    """登録済みの exchange が HNSW 検索でヒットする"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)

    vec = np.ones(384, dtype=np.float32)
    insert_exchange_with_vec(
        con, "ex1", "connection pool の修正", "pool_size=5 を追加", vec
    )
    con.close()

    results = search_hnsw(db_path, vec, limit=5)
    assert len(results) == 1
    assert results[0].exchange_id == "ex1"


def test_search_hnsw_respects_limit(tmp_path: Path) -> None:
    """limit パラメータが機能する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)

    for i in range(5):
        vec = np.full(384, float(i), dtype=np.float32)
        insert_exchange_with_vec(con, f"ex{i}", f"質問 {i}", f"回答 {i}", vec)
    con.close()

    results = search_hnsw(db_path, np.ones(384, dtype=np.float32), limit=3)
    assert len(results) <= 3


def test_search_hnsw_returns_search_result(tmp_path: Path) -> None:
    """SearchResult の型を持つ"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)

    vec = np.ones(384, dtype=np.float32)
    insert_exchange_with_vec(con, "ex1", "ユーザー発話", "エージェント応答", vec)
    con.close()

    results = search_hnsw(db_path, vec, limit=1)
    assert isinstance(results[0], SearchResult)
    assert results[0].user_content == "ユーザー発話"
    assert results[0].agent_content == "エージェント応答"


def test_search_hnsw_empty_db(tmp_path: Path) -> None:
    """登録なしは空リストを返す"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    vec = np.ones(384, dtype=np.float32)
    results = search_hnsw(db_path, vec, limit=5)
    assert results == []
