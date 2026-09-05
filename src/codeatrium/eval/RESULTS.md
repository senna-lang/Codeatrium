# symbol-recall v0 results — code→conversation lookup completeness

> Generated 2026-09-05 via `loci eval gen --dataset symbol-recall` + `loci
> eval run --dataset symbol-recall --adapter symbol`, against this
> repository's own `.codeatrium/memory.db` (beta dogfood corpus, single
> user/repo — see `docs/internal/EVAL-HARNESS.md` for methodology and
> disclosure caveats).

## What this measures

`loci context <file>:<symbol>` is "git blame for the conversation that
shaped this code" — a code→conversation lookup, not a search feature. It is
a different capability from keyword/semantic search (`loci search`,
BM25+HNSW/RRF), which answers "what did we decide about X?" from a free-text
question. The two solve different query shapes and are not compared against
each other here.

This report measures the lookup on its own terms: of the conversations
independently known to have **edited** a given symbol's file, how many does
`resolve_u1` (`code_edges`/`code_symbols`) actually recover?

## Gold definition — and a real bug this eval caught

Gold is built from `code_touches` (Edit/Write/MultiEdit — file-level edit
log, no symbol resolution) + a literal token match for the symbol in the
exchange's verbatim text + optional git-branch grounding. **Not**
`exchange_files`, which also records mere file *reads*.

The first version of this harness used `exchange_files`, and measured only
~9-11% recall/MRR. Investigating before accepting that number: of 518 gold
hits under the `exchange_files` definition, only **61 (12%)** were actually
edit-backed — the other 88% were conversations that merely *read* the file
and happened to mention the symbol's name, which `loci context` was never
designed to surface. Restricting gold to edit-backed exchanges only (43
queries) is the correct comparison, below.

## A second bug this eval caught: symbol-boundary drift

The first measurement against edit-only gold still landed at recall@10=0.70,
mrr@10=0.62 — better, but investigating *why* it wasn't higher surfaced a
second real bug in the production linking pipeline (`core/ingest.py`):
symbol resolution always read the **live, current** working-tree file at
index time, while `code_touches.new_start/new_lines` are frozen at the
moment of that historical edit. As a file keeps evolving after a touch, that
touch's line numbers drift out of alignment with the file's *current*
symbol boundaries — degrading a true symbol-level match down to a coarse
file-level "mention" edge. Concretely: only 9 of 1943 exchanges in this
corpus had any symbol-level (`granularity='line'`) edge before the fix.

**Fix**: resolve symbols against the git blob nearest each touch's own
timestamp (`_git_blob_near` + `_resolve_symbols_at` in `core/ingest.py`),
falling back to the live file only when git history is unavailable — this
can only improve alignment, never regress it. A one-time backfill
(`_backfill_touch_time_symbol_edges` in `db.py`, meta-flag gated like
`_backfill_legacy_code_edges`) applies the same fix retroactively to
touches already indexed under the old behavior. Applying it to this
project's real corpus raised symbol-level linkage from 9 to **14** distinct
exchanges (1050 → 1194 `code_edges` rows).

## Result

Corpus: 1961 exchanges, 689 sessions, **43 queries** (seed=42, fixed but
unused for sampling in v0 — dataset generation is deterministic, not
sampled).

| stage | recall@1 | recall@3 | recall@5 | recall@10 | mrr@10 |
|---|---|---|---|---|---|
| read-inclusive gold (bug 1, wrong denominator) | — | — | 0.090 | 0.095 | 0.111 |
| edit-only gold, pre-drift-fix | 0.439 | 0.652 | 0.652 | 0.698 | 0.624 |
| edit-only gold, post-drift-fix (**current**) | 0.524 | 0.760 | 0.768 | 0.795 | **0.709** |

The lookup now recovers the correct conversation within the top 10 results
**71% of the time on average** (MRR@10), covering **79.5% of gold
conversations** within the top 10 (Recall@10) — a meaningfully stronger,
and now largely git-history-grounded, completeness score.

## Remaining gap

One structural cause remains, confirmed by direct inspection: **harness
locator precision**. 23 of 40 touch-carrying exchanges in this corpus have
*only* `TextAnchor` locators (no line numbers at all — e.g. omp-pi's
undocumented patch DSL, or Claude's `Write` tool overwriting a whole file),
which can never intersect a symbol's line range regardless of how accurately
the file state is reconstructed. Closing this gap requires richer edit
capability from the harnesses themselves (structured diffs), not further
changes to the resolution logic here.

## Scope of this result

Single-user, single-repo, beta-period dogfood corpus (see disclosure
caveats in `docs/internal/EVAL-HARNESS.md`). Does not generalize. The small
query count (43) after restricting to edit-backed gold means per-query
variance is high; treat this as a first real measurement, not a precise
estimate.
