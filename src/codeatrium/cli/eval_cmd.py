"""loci eval — code→conversation lookup evaluation harness (v0: symbol-recall).

`loci eval gen` builds the symbol-recall dataset from the current project's
git history + `.codeatrium/memory.db`. `loci eval run` executes the `symbol`
adapter (the `loci context <file>:<symbol>` lookup) against it and reports
Recall@{1,3,5,10}/MRR@10 as a standalone completeness metric — not a
leaderboard against keyword/semantic search, which answers a different kind
of query and is out of scope for this harness. `loci eval report` is `run
--adapter all` rendered as markdown, for convenience.
"""

from __future__ import annotations

from typing import Annotated

import typer

eval_app = typer.Typer(help="Retrieval-quality evaluation harness")

_DEFAULT_SEED = 42
_DEFAULT_K = 10
_SUPPORTED_DATASETS = ("symbol-recall",)


@eval_app.command("gen")
def eval_gen(
    dataset: Annotated[
        str, typer.Option("--dataset", help="生成するデータセット名")
    ] = "symbol-recall",
    min_gold: Annotated[int, typer.Option("--min-gold")] = 1,
    max_gold: Annotated[int, typer.Option("--max-gold")] = 20,
) -> None:
    """symbol-recall データセットを実 DB + git 履歴から生成する。"""
    if dataset not in _SUPPORTED_DATASETS:
        typer.echo(
            f"Unsupported dataset: {dataset}. Choose one of: {', '.join(_SUPPORTED_DATASETS)}.",
            err=True,
        )
        raise typer.Exit(1)

    from codeatrium.db import get_connection
    from codeatrium.eval.datasets.schema import dataset_path, dump_dataset
    from codeatrium.eval.gen.gen_symbol_recall import generate_symbol_recall_queries
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    try:
        queries = generate_symbol_recall_queries(
            con, root, min_gold=min_gold, max_gold=max_gold
        )
    finally:
        con.close()

    out_path = dataset_path(dataset)
    dump_dataset(queries, out_path)
    typer.echo(f"Wrote {len(queries)} queries: {out_path}")


def _load_adapters(db):
    from codeatrium.eval.adapters.symbol import SymbolAdapter

    return {"symbol": SymbolAdapter(db)}


@eval_app.command("run")
def eval_run(
    dataset: Annotated[str, typer.Option("--dataset")] = "symbol-recall",
    adapter: Annotated[
        str, typer.Option("--adapter", help="all または symbol")
    ] = "all",
    k: Annotated[int, typer.Option("--k")] = _DEFAULT_K,
    seed: Annotated[int, typer.Option("--seed")] = _DEFAULT_SEED,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """dataset × adapter を実行し Recall@k / MRR@10 を表示する。"""
    from codeatrium.eval.datasets.schema import dataset_path, load_dataset
    from codeatrium.eval.report import render_json, render_markdown, score_runs
    from codeatrium.eval.runner import corpus_stats, run_adapters
    from codeatrium.paths import db_path, find_project_root

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    ds_path = dataset_path(dataset)
    if not ds_path.exists():
        typer.echo(
            f"Dataset not found: {ds_path}. Run `loci eval gen --dataset {dataset}` first.",
            err=True,
        )
        raise typer.Exit(1)
    queries = load_dataset(ds_path)

    registry = _load_adapters(db)
    if adapter == "all":
        adapters = list(registry.values())
    elif adapter in registry:
        adapters = [registry[adapter]]
    else:
        typer.echo(
            f"Unknown adapter: {adapter}. Choose one of: all, {', '.join(registry)}.",
            err=True,
        )
        raise typer.Exit(1)

    runs = run_adapters(adapters, queries, k)
    scores = score_runs(runs, queries)
    corpus = corpus_stats(db, queries)

    if json_output:
        typer.echo(render_json(scores, corpus, seed))
    else:
        typer.echo(render_markdown(scores, corpus, seed))


@eval_app.command("report")
def eval_report(
    dataset: Annotated[str, typer.Option("--dataset")] = "symbol-recall",
    k: Annotated[int, typer.Option("--k")] = _DEFAULT_K,
    seed: Annotated[int, typer.Option("--seed")] = _DEFAULT_SEED,
) -> None:
    """`loci eval run --adapter all` の md レポート表示（別名）。"""
    eval_run(dataset=dataset, adapter="all", k=k, seed=seed, json_output=False)
