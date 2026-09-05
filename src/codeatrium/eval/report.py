"""Score adapter runs and render md/json completeness tables (E5).

Not a leaderboard: the code→conversation lookup feature (`symbol`) answers a
different query shape than keyword/semantic search (`loci search`), so there
is no meaningful baseline to diff against here. Recall@{1,3,5,10}/MRR@10
measure the lookup's own completeness against gold — "of the conversations
that actually touched this code, how many does the lookup recover" — not
relative standing against unrelated retrieval methods.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from codeatrium.eval.datasets.schema import Query
from codeatrium.eval.metrics import mrr, recall_at_k
from codeatrium.eval.runner import AdapterRunResult, CorpusStats

RECALL_KS: tuple[int, ...] = (1, 3, 5, 10)
MRR_K = 10


@dataclass(frozen=True)
class AdapterScore:
    adapter_id: str
    recall_at: dict[int, float]
    mrr_at_10: float


def score_adapter(run: AdapterRunResult, queries_by_id: dict[str, Query]) -> AdapterScore:
    recall_scores: dict[int, list[float]] = {k: [] for k in RECALL_KS}
    mrr_scores: list[float] = []
    for result in run.results:
        gold = queries_by_id[result.query_id].gold_exchange_ids
        ranked = list(result.ranked_exchange_ids)
        for k in RECALL_KS:
            recall_scores[k].append(recall_at_k(ranked, gold, k))
        mrr_scores.append(mrr(ranked, gold, MRR_K))

    recall_at = (
        {k: sum(v) / len(v) for k, v in recall_scores.items()}
        if mrr_scores
        else dict.fromkeys(RECALL_KS, 0.0)
    )
    mrr_at_10 = sum(mrr_scores) / len(mrr_scores) if mrr_scores else 0.0
    return AdapterScore(adapter_id=run.adapter_id, recall_at=recall_at, mrr_at_10=mrr_at_10)


def score_runs(runs: list[AdapterRunResult], queries: list[Query]) -> list[AdapterScore]:
    queries_by_id = {q.id: q for q in queries}
    return [score_adapter(run, queries_by_id) for run in runs]


def render_markdown(scores: list[AdapterScore], corpus: CorpusStats, seed: int) -> str:
    lines = [
        f"Corpus: {corpus.exchange_count} exchanges, {corpus.session_count} sessions, "
        f"{corpus.query_count} queries (seed={seed})",
        "",
        "| adapter | recall@1 | recall@3 | recall@5 | recall@10 | mrr@10 |",
        "|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.adapter_id} | {s.recall_at[1]:.3f} | {s.recall_at[3]:.3f} | "
            f"{s.recall_at[5]:.3f} | {s.recall_at[10]:.3f} | {s.mrr_at_10:.3f} |"
        )
    return "\n".join(lines)


def render_json(scores: list[AdapterScore], corpus: CorpusStats, seed: int) -> str:
    payload = {
        "corpus": {
            "exchange_count": corpus.exchange_count,
            "session_count": corpus.session_count,
            "query_count": corpus.query_count,
        },
        "seed": seed,
        "adapters": [
            {
                "adapter_id": s.adapter_id,
                "recall_at": {str(k): v for k, v in s.recall_at.items()},
                "mrr_at_10": s.mrr_at_10,
            }
            for s in scores
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
