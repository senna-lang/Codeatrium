"""recall_at_k / mrr — known ranked/gold pairs against hand-computed values (E4)."""

from __future__ import annotations

import pytest

from codeatrium.eval.metrics import mrr, recall_at_k


def test_recall_at_k_counts_gold_hits_within_top_k() -> None:
    ranked = ["e1", "e2", "e3", "e4"]
    gold = {"e2", "e4", "e5"}
    assert recall_at_k(ranked, gold, k=2) == pytest.approx(1 / 3)
    assert recall_at_k(ranked, gold, k=4) == pytest.approx(2 / 3)


def test_recall_at_k_full_gold_coverage_is_one() -> None:
    assert recall_at_k(["e1", "e2"], {"e1", "e2"}, k=2) == 1.0


def test_recall_at_k_no_hits_is_zero() -> None:
    assert recall_at_k(["e1", "e2"], {"e9"}, k=10) == 0.0


def test_recall_at_k_rejects_empty_gold() -> None:
    with pytest.raises(ValueError, match="gold"):
        recall_at_k(["e1"], set(), k=5)


def test_recall_at_k_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError, match="k"):
        recall_at_k(["e1"], {"e1"}, k=0)


def test_mrr_returns_reciprocal_of_first_hit_rank() -> None:
    assert mrr(["e1", "e2", "e3"], {"e3"}, k=10) == pytest.approx(1 / 3)
    assert mrr(["e1", "e2", "e3"], {"e1"}, k=10) == pytest.approx(1.0)


def test_mrr_ignores_hits_beyond_k() -> None:
    assert mrr(["e1", "e2", "e3"], {"e3"}, k=2) == 0.0


def test_mrr_no_hit_is_zero() -> None:
    assert mrr(["e1", "e2"], {"e9"}, k=10) == 0.0


def test_mrr_rejects_empty_gold() -> None:
    with pytest.raises(ValueError, match="gold"):
        mrr(["e1"], set(), k=5)
