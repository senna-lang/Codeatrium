"""Regression gate for symbol-recall — baseline compare + synthetic fixture (issue #37).

The gate is a completeness check against a committed synthetic-fixture baseline,
not a BM25/HNSW/RRF leaderboard. MRR@10 may not drop more than an absolute
tolerance of 0.01; improvement and ties pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection
from codeatrium.eval.adapters.symbol import SymbolAdapter
from codeatrium.eval.datasets.schema import Query
from codeatrium.eval.fixture import build_symbol_recall_fixture
from codeatrium.eval.gate import (
    DEFAULT_TOLERANCE_ABS,
    Baseline,
    compare_to_baseline,
    load_baseline,
)
from codeatrium.eval.report import AdapterScore, score_runs
from codeatrium.eval.runner import run_adapters

runner = CliRunner()


def _score(mrr_at_10: float, recall: float = 1.0) -> AdapterScore:
    return AdapterScore(
        adapter_id="symbol",
        recall_at={1: recall, 3: recall, 5: recall, 10: recall},
        mrr_at_10=mrr_at_10,
    )


def _baseline(mrr_at_10: float, recall: float = 1.0) -> Baseline:
    return Baseline(
        dataset="symbol-recall",
        adapter="symbol",
        seed=42,
        k=10,
        tolerance_abs_mrr_at_10=DEFAULT_TOLERANCE_ABS,
        recall_at={1: recall, 3: recall, 5: recall, 10: recall},
        mrr_at_10=mrr_at_10,
    )


def test_compare_to_baseline_tie_passes() -> None:
    result = compare_to_baseline(_score(1.0), _baseline(1.0), tolerance=0.01)
    assert result.failed is False
    assert result.delta_mrr_at_10 == 0.0


def test_compare_to_baseline_improvement_passes() -> None:
    result = compare_to_baseline(_score(1.0), _baseline(0.5), tolerance=0.01)
    assert result.failed is False
    assert result.delta_mrr_at_10 == 0.5


def test_compare_to_baseline_regression_beyond_tolerance_fails() -> None:
    result = compare_to_baseline(_score(0.5), _baseline(1.0), tolerance=0.01)
    assert result.failed is True
    assert result.delta_mrr_at_10 == -0.5


def test_compare_to_baseline_regression_within_tolerance_passes() -> None:
    result = compare_to_baseline(_score(0.995), _baseline(1.0), tolerance=0.01)
    assert result.failed is False


def test_gate_pass_fail_on_improvement_regression_tie(tmp_path: Path) -> None:
    """Seed a baseline from the synthetic fixture, then verify tie / improvement / regression."""
    db = build_symbol_recall_fixture(tmp_path)
    queries = [
        Query(
            id="q-list-dir",
            kind="symbol",
            value="src/widget.py::list_dir",
            gold_exchange_ids=("ex-list-dir",),
        ),
        Query(
            id="q-sort-items",
            kind="symbol",
            value="src/widget.py::sort_items",
            gold_exchange_ids=("ex-sort-items",),
        ),
    ]
    good_runs = run_adapters([SymbolAdapter(db)], queries, k=10)
    good = score_runs(good_runs, queries)[0]
    assert good.mrr_at_10 == 1.0

    baseline = _baseline(good.mrr_at_10)

    tie = compare_to_baseline(good, baseline, tolerance=DEFAULT_TOLERANCE_ABS)
    assert tie.failed is False

    weaker = _baseline(good.mrr_at_10 - 0.5)
    improved = compare_to_baseline(good, weaker, tolerance=DEFAULT_TOLERANCE_ABS)
    assert improved.failed is False

    con = get_connection(db)
    con.execute("DELETE FROM code_edges")
    con.commit()
    con.close()
    bad_runs = run_adapters([SymbolAdapter(db)], queries, k=10)
    bad = score_runs(bad_runs, queries)[0]
    assert bad.mrr_at_10 < good.mrr_at_10
    regress = compare_to_baseline(bad, baseline, tolerance=DEFAULT_TOLERANCE_ABS)
    assert regress.failed is True


def test_synthetic_fixture_needs_no_network(tmp_path: Path) -> None:
    db = build_symbol_recall_fixture(tmp_path)
    assert db.exists()
    assert (tmp_path / "src" / "widget.py").exists()
    assert (tmp_path / ".git").exists()
    con = get_connection(db)
    try:
        n_ex = con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
        n_edges = con.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
        n_syms = con.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0]
    finally:
        con.close()
    assert n_ex >= 2
    assert n_edges >= 2
    assert n_syms >= 2


def test_load_baseline_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "dataset": "symbol-recall",
                "adapter": "symbol",
                "seed": 42,
                "k": 10,
                "tolerance_abs_mrr_at_10": 0.01,
                "metrics": {
                    "recall_at": {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
                    "mrr_at_10": 1.0,
                },
            }
        )
    )
    loaded = load_baseline(path)
    assert loaded.mrr_at_10 == 1.0
    assert loaded.tolerance_abs_mrr_at_10 == 0.01
    assert loaded.adapter == "symbol"


def test_eval_gate_cli_passes_against_matching_baseline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "dataset": "symbol-recall",
                "adapter": "symbol",
                "seed": 42,
                "k": 10,
                "tolerance_abs_mrr_at_10": 0.01,
                "metrics": {
                    "recall_at": {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
                    "mrr_at_10": 1.0,
                },
            }
        )
    )
    result = runner.invoke(app, ["eval", "gate", "--baseline", str(baseline), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["failed"] is False
    assert payload["adapter"] == "symbol"
    assert "current_mrr_at_10" in payload
    assert "baseline_mrr_at_10" in payload
    assert payload["tolerance_abs_mrr_at_10"] == 0.01


def test_eval_gate_cli_fails_when_current_regresses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "dataset": "symbol-recall",
                "adapter": "symbol",
                "seed": 42,
                "k": 10,
                "tolerance_abs_mrr_at_10": 0.01,
                "metrics": {
                    "recall_at": {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
                    "mrr_at_10": 1.0,
                },
            }
        )
    )

    def _regressed(_queries, _db):
        return AdapterScore(
            adapter_id="symbol",
            recall_at={1: 0.0, 3: 0.0, 5: 0.0, 10: 0.0},
            mrr_at_10=0.0,
        )

    monkeypatch.setattr("codeatrium.cli.eval_cmd._score_fixture_run", _regressed)
    result = runner.invoke(app, ["eval", "gate", "--baseline", str(baseline), "--json"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout or result.output)
    assert payload["failed"] is True


def test_eval_gate_cli_missing_baseline_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["eval", "gate", "--baseline", str(tmp_path / "nope.json")]
    )
    assert result.exit_code != 0
    assert "baseline" in result.output.lower()


def test_eval_gate_cli_text_mentions_mrr(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "dataset": "symbol-recall",
                "adapter": "symbol",
                "seed": 42,
                "k": 10,
                "tolerance_abs_mrr_at_10": 0.01,
                "metrics": {
                    "recall_at": {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
                    "mrr_at_10": 1.0,
                },
            }
        )
    )
    result = runner.invoke(app, ["eval", "gate", "--baseline", str(baseline)])
    assert result.exit_code == 0, result.output
    assert "mrr" in result.output.lower()
