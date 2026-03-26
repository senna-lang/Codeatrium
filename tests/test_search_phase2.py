"""
Phase 2 検索テスト: BM25・CombMNZ・search_combined
embedding は固定ベクトルで代替してモデルロードを避ける
"""

import struct
from pathlib import Path

import numpy as np

from logo.db import get_connection, init_db
from logo.search import BM25Result, CombMNZResult, combmnz, search_bm25, search_combined

LONG_TEXT = "connection pool " * 10


def _insert_exchange(con, ex_id, user_content, agent_content):
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?,?)",
        ("conv1", "/path/to.jsonl"),
    )
    con.execute(
        """
        INSERT OR IGNORE INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content)
        VALUES (?,?,?,?,?,?)
        """,
        (ex_id, "conv1", 0, 3, user_content, agent_content),
    )
    con.commit()


def _insert_palace(con, palace_id, exchange_id, bm25_text, exchange_core, vec):
    con.execute(
        """
        INSERT OR IGNORE INTO palace_objects
            (id, exchange_id, exchange_core, specific_context, distill_text, bm25_text)
        VALUES (?,?,?,?,?,?)
        """,
        (palace_id, exchange_id, exchange_core, "detail", exchange_core, bm25_text),
    )
    blob = struct.pack(f"{len(vec)}f", *vec.tolist())
    con.execute(
        "INSERT OR IGNORE INTO vec_palace (palace_id, embedding) VALUES (?,?)",
        (palace_id, blob),
    )
    con.commit()


# --- search_bm25 ---


def test_search_bm25_returns_results(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool_size=5 を追加した")
    con.close()

    results = search_bm25(db_path, "connection pool", limit=5)
    assert len(results) >= 1
    assert results[0].exchange_id == "ex1"


def test_search_bm25_empty_db_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    results = search_bm25(db_path, "query", limit=5)
    assert results == []


def test_search_bm25_no_match_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool_size=5 を追加した")
    con.close()

    results = search_bm25(db_path, "xyznonexistentword123", limit=5)
    assert results == []


def test_search_bm25_returns_bm25result(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool_size=5 を追加した")
    con.close()

    results = search_bm25(db_path, "connection", limit=5)
    assert isinstance(results[0], BM25Result)
    assert results[0].bm25_score > 0


# --- combmnz ---


def test_combmnz_bm25_only() -> None:
    bm25 = [BM25Result(exchange_id="ex1", user_content="u", agent_content="a", bm25_score=2.0)]
    results = combmnz(bm25, [], limit=5)
    assert len(results) == 1
    assert results[0].exchange_id == "ex1"


def test_combmnz_returns_combmnz_result() -> None:
    bm25 = [BM25Result(exchange_id="ex1", user_content="u", agent_content="a", bm25_score=1.0)]
    results = combmnz(bm25, [], limit=5)
    assert isinstance(results[0], CombMNZResult)


def test_combmnz_both_hits_scores_higher() -> None:
    """BM25 + HNSW 両方ヒットは片方ヒットより高スコア"""
    from logo.search import HNSWPalaceResult

    bm25 = [
        BM25Result(exchange_id="ex1", user_content="u", agent_content="a", bm25_score=1.0),
        BM25Result(exchange_id="ex2", user_content="u", agent_content="a", bm25_score=0.5),
    ]
    hnsw = [
        HNSWPalaceResult(
            exchange_id="ex1",
            user_content="u",
            agent_content="a",
            exchange_core="core",
            specific_context="ctx",
            distance=0.1,
        )
    ]
    results = combmnz(bm25, hnsw, limit=5)
    # ex1 はどちらにもヒットするので ex2 より高スコアになるはず
    ids = [r.exchange_id for r in results]
    assert ids[0] == "ex1"


def test_combmnz_includes_exchange_core_when_available() -> None:
    from logo.search import HNSWPalaceResult

    bm25 = []
    hnsw = [
        HNSWPalaceResult(
            exchange_id="ex1",
            user_content="u",
            agent_content="a",
            exchange_core="connection pool を修正した",
            specific_context="pool_size=5",
            distance=0.1,
        )
    ]
    results = combmnz(bm25, hnsw, limit=5)
    assert results[0].exchange_core == "connection pool を修正した"


def test_combmnz_empty_both() -> None:
    results = combmnz([], [], limit=5)
    assert results == []


def test_combmnz_respects_limit() -> None:
    bm25 = [
        BM25Result(exchange_id=f"ex{i}", user_content="u", agent_content="a", bm25_score=float(i))
        for i in range(10)
    ]
    results = combmnz(bm25, [], limit=3)
    assert len(results) <= 3


# --- search_combined ---


def test_search_combined_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    vec = np.ones(384, dtype=np.float32)
    results = search_combined(db_path, "query", vec, limit=5)
    assert results == []


def test_search_combined_bm25_hit(tmp_path: Path) -> None:
    """palace なし・BM25 のみでも結果が返る"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool_size=5 を追加した")
    con.close()

    vec = np.ones(384, dtype=np.float32)
    results = search_combined(db_path, "connection pool", vec, limit=5)
    assert len(results) >= 1
    assert results[0].exchange_id == "ex1"


def test_search_combined_with_palace(tmp_path: Path) -> None:
    """palace あり・HNSW(D) ヒット"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool_size=5 を追加した")
    _insert_palace(
        con, "p1", "ex1",
        "connection pool pool_size db-pool DB Pool",
        "connection pool を修正した",
        np.ones(384, dtype=np.float32),
    )
    con.close()

    vec = np.ones(384, dtype=np.float32)
    results = search_combined(db_path, "connection pool", vec, limit=5)
    assert any(r.exchange_id == "ex1" for r in results)
