"""
Phase 2 検索テスト: BM25・RRF・search_combined
embedding は固定ベクトルで代替してモデルロードを避ける
"""

import sqlite3
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from codeatrium.db import get_connection, init_db
from codeatrium.search import (
    BM25Result,
    FusedResult,
    HNSWPalaceResult,
    rrf,
    search_bm25,
    search_combined,
    search_hnsw_palace,
)

LONG_TEXT = "connection pool " * 10


def _insert_exchange(con, ex_id, user_content, agent_content, conv_id="conv1", git_branch: str | None = None):
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
            (id, conversation_id, ply_start, ply_end, user_content, agent_content, git_branch)
        VALUES (?,?,?,?,?,?,?)
        """,
        (ex_id, conv_id, 2, 5, user_content, agent_content, git_branch),
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


# --- connection leak tests ---


def test_search_bm25_connection_leak(tmp_path: Path) -> None:
    """search_bm25 は例外時も connection を close する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    fake_con = MagicMock()
    fake_con.execute.side_effect = RuntimeError("test error")

    with patch("codeatrium.search.get_connection", return_value=fake_con):
        with pytest.raises(RuntimeError):
            search_bm25(db_path, "query")

    assert fake_con.close.called


def test_search_hnsw_connection_leak(tmp_path: Path) -> None:
    """search_hnsw_palace は例外時も connection を close する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)

    fake_con = MagicMock()
    fake_con.execute.side_effect = RuntimeError("test error")

    with patch("codeatrium.search.get_connection", return_value=fake_con):
        with pytest.raises(RuntimeError):
            search_hnsw_palace(db_path, np.ones(384, dtype=np.float32))

    assert fake_con.close.called


def test_search_hnsw_branch_filter_correct_binding(tmp_path: Path) -> None:
    """search_hnsw_palace with branch filter binds parameters correctly.

    Verifies that the branch string is bound to the LIKE ? placeholder,
    not to the min_exchanges >= ? integer placeholder. If parameter order
    is wrong, SQLite will attempt type coercion and either fail or produce
    incorrect results.
    """
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)

    # Insert two exchanges with different git_branch values
    _insert_exchange(con, "hnsw-branch-a", "connection pool management system " * 3, "response a", conv_id="hnsw-conv-branch", git_branch="branch-a")
    _insert_exchange(con, "hnsw-branch-b", "database query optimization " * 3, "response b", conv_id="hnsw-conv-branch", git_branch="branch-b")

    # Insert palace_objects and vec_palace for both exchanges
    query_vec = np.zeros(384, dtype=np.float32)
    _insert_palace(con, "palace-a", "hnsw-branch-a", "palace core a", query_vec)
    _insert_palace(con, "palace-b", "hnsw-branch-b", "palace core b", query_vec)

    con.close()

    # Call search_hnsw_palace with branch filter
    # If parameter binding is wrong, this will either raise an exception
    # or return results from both branches
    results = search_hnsw_palace(db_path, query_vec, limit=10, min_exchanges=2, branch="branch-a")

    # Verify that only branch-a results are returned (or empty if no match)
    for result in results:
        assert result.exchange_id == "hnsw-branch-a", f"Expected only 'hnsw-branch-a', got {result.exchange_id}"


def test_search_combined_shares_single_connection(tmp_path: Path) -> None:
    """search_combined は bm25/hnsw/enrich で get_connection を1回だけ呼ぶ
    （1検索3コネクション問題の回帰防止、issue #25）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response")
    con.close()

    with patch(
        "codeatrium.search.get_connection", wraps=get_connection
    ) as mock_get_connection:
        results = search_combined(
            db_path, "connection pool", np.ones(384, dtype=np.float32), limit=5
        )

    assert mock_get_connection.call_count == 1
    assert any(r.exchange_id == "ex1" for r in results)


def test_search_combined_connection_leak(tmp_path: Path) -> None:
    """search_combined は enrich 時に exception が出ても共有 connection を close する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response")

    class _CloseCountingConnection:
        """sqlite3.Connection は C 拡張型でインスタンス属性の再代入を許さない
        （`close` の再代入は AttributeError）ため、close 呼び出し回数を数える
        委譲プロキシで代用する。"""

        def __init__(self, real_con: sqlite3.Connection) -> None:
            self._real_con = real_con
            self.close_calls = 0

        def __getattr__(self, name: str):
            return getattr(self._real_con, name)

        def close(self) -> None:
            self.close_calls += 1
            self._real_con.close()

    proxy = _CloseCountingConnection(con)

    with patch(
        "codeatrium.search.get_connection", return_value=proxy
    ), patch(
        "codeatrium.search._enrich_results", side_effect=RuntimeError("enrich failed")
    ):
        with pytest.raises(RuntimeError):
            search_combined(
                db_path, "connection pool", np.ones(384, dtype=np.float32), limit=5
            )

    assert proxy.close_calls == 1


def test_search_bm25_branch_filter(tmp_path: Path) -> None:
    """branch フィルタで指定されたブランチのみ返される"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response", git_branch="branch-a")
    _insert_exchange(con, "ex2", LONG_TEXT, "pool response", conv_id="conv2", git_branch="branch-b")
    con.close()

    results = search_bm25(db_path, "connection pool", branch="branch-a")
    assert len(results) == 1
    assert results[0].exchange_id == "ex1"


def test_search_bm25_branch_partial_match(tmp_path: Path) -> None:
    """branch フィルタは部分一致で動作する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response", git_branch="release/1.0-hardening")
    con.close()

    results = search_bm25(db_path, "connection pool", branch="1.0-hardening")
    assert len(results) == 1
    assert results[0].exchange_id == "ex1"


def test_search_combined_branch_filter(tmp_path: Path) -> None:
    """search_combined で branch フィルタが機能する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response", git_branch="target-branch")
    _insert_exchange(con, "ex2", LONG_TEXT, "pool response", conv_id="conv2", git_branch="other-branch")
    con.close()

    vec = np.ones(384, dtype=np.float32)
    results = search_combined(db_path, "connection pool", vec, branch="target-branch")
    assert any(r.exchange_id == "ex1" for r in results)
    assert not any(r.exchange_id == "ex2" for r in results)


def test_enrich_results_populates_git_branch(tmp_path: Path) -> None:
    """_enrich_results が git_branch を正しく付加する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response", git_branch="feature-x")
    con.close()

    vec = np.ones(384, dtype=np.float32)
    results = search_combined(db_path, "connection pool", vec, limit=5)
    assert len(results) >= 1
    assert results[0].git_branch == "feature-x"


# ---- issue #18: branch LIKE の未エスケープ + KNN→フィルタ順 recall 低下 ----


def test_search_bm25_branch_underscore_is_not_a_wildcard(tmp_path: Path) -> None:
    """`_` は LIKE の1文字ワイルドカードなので、未エスケープだと無関係な
    ブランチにも部分一致してしまう（issue #18）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "exact", LONG_TEXT, "pool response", conv_id="c1", git_branch="release_1.0")
    _insert_exchange(con, "wrong", LONG_TEXT, "pool response", conv_id="c2", git_branch="releaseX1.0")
    con.close()

    results = search_bm25(db_path, "connection pool", branch="release_1.0")

    assert {r.exchange_id for r in results} == {"exact"}


def test_search_bm25_branch_percent_is_not_a_wildcard(tmp_path: Path) -> None:
    """`%` は LIKE の任意長ワイルドカードなので、未エスケープだと無関係な
    ブランチにも一致してしまう（issue #18）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "exact", LONG_TEXT, "pool response", conv_id="c1", git_branch="rel%main")
    _insert_exchange(con, "wrong", LONG_TEXT, "pool response", conv_id="c2", git_branch="relXXXXmain")
    con.close()

    results = search_bm25(db_path, "connection pool", branch="rel%main")

    assert {r.exchange_id for r in results} == {"exact"}


def test_search_bm25_branch_still_matches_as_substring(tmp_path: Path) -> None:
    """ワイルドカード非対象の入力では、従来通り部分一致で動作し続ける（回帰防止）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    _insert_exchange(con, "ex1", LONG_TEXT, "pool response", git_branch="release/1.0-hardening")
    con.close()

    results = search_bm25(db_path, "connection pool", branch="1.0-hardening")
    assert len(results) == 1
    assert results[0].exchange_id == "ex1"


def test_search_hnsw_branch_underscore_is_not_a_wildcard(tmp_path: Path) -> None:
    """search_hnsw_palace の branch フィルタでも `_` を literal として扱う（issue #18）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)
    query_vec = np.ones(384, dtype=np.float32)
    _insert_exchange(con, "exact", LONG_TEXT, "resp", conv_id="c1", git_branch="release_1.0")
    _insert_palace(con, "p-exact", "exact", "core", query_vec)
    _insert_exchange(con, "wrong", LONG_TEXT, "resp", conv_id="c2", git_branch="releaseX1.0")
    _insert_palace(con, "p-wrong", "wrong", "core", query_vec)
    con.close()

    results = search_hnsw_palace(db_path, query_vec, limit=10, min_exchanges=2, branch="release_1.0")

    assert {r.exchange_id for r in results} == {"exact"}


def test_search_hnsw_palace_branch_filter_after_knn_cutoff_loses_recall(tmp_path: Path) -> None:
    """KNN の候補が limit 件で打ち切られてから branch/min_exchanges で絞ると、
    目的の結果が近傍圏外に押し出されて recall が失われる（issue #18）。
    候補生成側の k は最終 limit より大きく取り、フィルタ後に outer LIMIT で
    絞らなければならない。"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)

    query_vec = np.ones(384, dtype=np.float32)
    target_vec = np.full(384, 0.5, dtype=np.float32)

    # クエリベクトルに極めて近いが、目的ブランチではないノイズを複数件用意する。
    # limit=2 のとき、これらだけで KNN の上位2枠が埋まってしまう。
    for i in range(3):
        _insert_exchange(con, f"noise-{i}", LONG_TEXT, "resp", conv_id=f"noise-conv-{i}", git_branch="other-branch")
        _insert_palace(con, f"noise-palace-{i}", f"noise-{i}", "noise", query_vec)

    # 目的ブランチの唯一の候補は、クエリベクトルからやや離れている
    _insert_exchange(con, "target", LONG_TEXT, "resp", conv_id="target-conv", git_branch="feature-target")
    _insert_palace(con, "target-palace", "target", "target core", target_vec)
    con.close()

    results = search_hnsw_palace(db_path, query_vec, limit=2, min_exchanges=2, branch="feature-target")

    assert len(results) == 1
    assert results[0].exchange_id == "target"


def test_search_hnsw_palace_adaptive_widening_beyond_fixed_multiplier(tmp_path: Path) -> None:
    """フィルタの選択率が固定倍率（5x limit）を超えて厳しい場合でも、k を
    適応的に広げて最良の候補を見つける（issue #18 レビュー指摘: 固定倍率
    だけだと閾値が変わるだけで同じ recall 消失が再発する）。

    limit=2 に対しノイズを 5*limit=10 件より多い12件用意することで、初回の
    固定候補プール(k=10)だけでは目的ブランチの候補に届かないシナリオを作る。
    """
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    con = get_connection(db_path)

    query_vec = np.ones(384, dtype=np.float32)
    target_vec = np.full(384, 0.5, dtype=np.float32)

    limit = 2
    noise_count = limit * 5 + 2  # 初期候補プール(limit*5=10)を超える件数
    for i in range(noise_count):
        _insert_exchange(con, f"noise-{i}", LONG_TEXT, "resp", conv_id=f"noise-conv-{i}", git_branch="other-branch")
        _insert_palace(con, f"noise-palace-{i}", f"noise-{i}", "noise", query_vec)

    _insert_exchange(con, "target", LONG_TEXT, "resp", conv_id="target-conv", git_branch="feature-target")
    _insert_palace(con, "target-palace", "target", "target core", target_vec)
    con.close()

    results = search_hnsw_palace(db_path, query_vec, limit=limit, min_exchanges=2, branch="feature-target")

    assert len(results) == 1
    assert results[0].exchange_id == "target"