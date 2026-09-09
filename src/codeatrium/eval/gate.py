"""Regression gate for the symbol-recall completeness eval (issue #37).

Compares a fresh `symbol` adapter run against a committed baseline. The only
fail condition is MRR@10 dropping by more than an *absolute* tolerance
(`DEFAULT_TOLERANCE_ABS` = 0.01). Improvement and ties pass. This is not a
BM25/HNSW/RRF leaderboard — keyword-recall is out of scope for v0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from codeatrium.eval.report import AdapterScore

DEFAULT_TOLERANCE_ABS = 0.01
DEFAULT_BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


@dataclass(frozen=True)
class Baseline:
    dataset: str
    adapter: str
    seed: int
    k: int
    tolerance_abs_mrr_at_10: float
    recall_at: dict[int, float]
    mrr_at_10: float


@dataclass(frozen=True)
class GateResult:
    failed: bool
    delta_mrr_at_10: float
    tolerance: float


def load_baseline(path: Path) -> Baseline:
    """Load a committed baseline JSON (see `baseline.json` for the schema)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    metrics = raw["metrics"]
    recall_raw = metrics["recall_at"]
    return Baseline(
        dataset=raw["dataset"],
        adapter=raw["adapter"],
        seed=int(raw["seed"]),
        k=int(raw["k"]),
        tolerance_abs_mrr_at_10=float(raw["tolerance_abs_mrr_at_10"]),
        recall_at={int(k): float(v) for k, v in recall_raw.items()},
        mrr_at_10=float(metrics["mrr_at_10"]),
    )


def compare_to_baseline(
    score: AdapterScore,
    baseline: Baseline,
    tolerance: float | None = None,
) -> GateResult:
    """Fail only when MRR@10 regresses by more than `tolerance` (absolute)."""
    tol = DEFAULT_TOLERANCE_ABS if tolerance is None else tolerance
    delta = score.mrr_at_10 - baseline.mrr_at_10
    return GateResult(failed=delta < -tol, delta_mrr_at_10=delta, tolerance=tol)
