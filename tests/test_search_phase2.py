"""
Phase 2 検索テスト: BM25・RRF・search_combined
embedding は固定ベクトルで代替してモデルロードを避ける
"""

import struct
from pathlib import Path

import numpy as np

from codeatrium.db import get_connection, init_db
from codeatrium.search import (
    BM25Result,
    FusedResult,
    HNSWPalaceResult,
    rrf,
    search_bm25,
    search_combined,
)

LONG_TEXT = "connection pool " * 10


def _insert_exchange(con, ex_id, user_content, agent_content, conv_id="conv1"):
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?,?)",
        (conv_id, f"/path/{conv_id}.jsonl"),
    )
    # 会話に2件以上の exchange を確保（min_exchanges=2 フィルタ対策）
    con.execute(
        """
        INSERT OR IGNORE INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content)
        VALUES (?,?,?,?,?,?)
        """,
        (f"_pad_{conv_id}", conv_id, 0, 1, "padding", "padding"),
    )
    con.execute(
        """
        INSERT OR IGNORE INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content)
        VALUES (?,?,?,?,?,?)
        """,
        (ex_id, conv_id, 2, 5, user_content, agent_content),
    )
    con.commit()


def _insert_palace(con, palace_id, exchange_id, exchange_core, vec):
    con.execute(
        """
        INSERT OR IGNORE INTO palace_objects
            (id, exchange_id, exchange_core, specific_context, distill_text)
        VALUES (?,?,?,?,?)
        """,
        (palace_id, exchange_id, exchange_core, "detail", exchange_core),
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


# --- rrf ---


def test_rrf_bm25_only() -> None:
    bm25 = [
        BM25Result(
            exchange_id="ex1", user_content="u", agent_content="a", bm25_score=2.0
        )
    ]
    results = rrf(bm25, [], limit=5)
    assert len(results) == 1
    assert results[0].exchange_id == "ex1"


def test_rrf_returns_fused_result() -> None:
    bm25 = [
        BM25Result(
            exchange_id="ex1", user_content="u", agent_content="a", bm25_score=1.0
        )
    ]
    results = rrf(bm25, [], limit=5)
    assert isinstance(results[0], FusedResult)


def test_rrf_both_lists_scores_higher() -> None:
    """両リストにヒットした exchange は片方のみより高スコア"""
    bm25 = [
        BM25Result(
            exchange_id="ex1", user_content="u", agent_content="a", bm25_score=1.0
        ),
        BM25Result(
            exchange_id="ex2", user_content="u", agent_content="a", bm25_score=0.5
        ),
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
    results = rrf(bm25, hnsw, limit=5)
    # ex1 は両リストにヒット → ex2（BM25 のみ）より上位
    assert results[0].exchange_id == "ex1"


def test_rrf_includes_exchange_core_from_hnsw() -> None:
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
    results = rrf([], hnsw, limit=5)
    assert results[0].exchange_core == "connection pool を修正した"


def test_rrf_empty_both() -> None:
    results = rrf([], [], limit=5)
    assert results == []


def test_rrf_respects_limit() -> None:
    bm25 = [
        BM25Result(
            exchange_id=f"ex{i}",
            user_content="u",
            agent_content="a",
            bm25_score=float(i),
        )
        for i in range(10)
    ]
    results = rrf(bm25, [], limit=3)
    assert len(results) <= 3


def test_rrf_score_decreases_with_rank() -> None:
    """順位が下がるほどスコアが下がる（RRF の基本性質）"""
    bm25 = [
        BM25Result(
            exchange_id="ex1", user_content="u", agent_content="a", bm25_score=10.0
        ),
        BM25Result(
            exchange_id="ex2", user_content="u", agent_content="a", bm25_score=1.0
        ),
    ]
    results = rrf(bm25, [], limit=5)
    assert results[0].score > results[1].score


# --- min_exchanges フィルタ ---


def test_search_bm25_excludes_single_exchange_sessions(tmp_path: Path) -> None:
    """1件しかない会話は min_exchanges=2 でフィルタされる"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    # 1 exchange のみの会話
    con.execute(
        "INSERT INTO conversations (id, source_path) VALUES (?,?)",
        ("solo", "/path/solo.jsonl"),
    )
    con.execute(
        """
        INSERT INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content)
        VALUES (?,?,?,?,?,?)
        """,
        ("ex_solo", "solo", 0, 3, LONG_TEXT, "solo response"),
    )
    con.commit()
    con.close()

    results = search_bm25(db_path, "connection pool", limit=5, min_exchanges=2)
    assert results == []


def test_search_bm25_includes_multi_exchange_sessions(tmp_path: Path) -> None:
    """2件以上ある会話は min_exchanges=2 で返される"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response")
    con.close()

    results = search_bm25(db_path, "connection pool", limit=5, min_exchanges=2)
    assert len(results) >= 1


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
        con,
        "p1",
        "ex1",
        "connection pool を修正した",
        np.ones(384, dtype=np.float32),
    )
    con.close()

    vec = np.ones(384, dtype=np.float32)
    results = search_combined(db_path, "connection pool", vec, limit=5)
    assert any(r.exchange_id == "ex1" for r in results)
