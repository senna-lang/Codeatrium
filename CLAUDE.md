# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`logosyncs` is a CLI-first memory layer for AI coding agents. The command is `logo`. It lets agents like Claude Code search past conversations, retrieve code locations (file + line + symbol), and link conversation history to code symbols.

Primary user is **the agent itself**, not a human. The tool is invoked via `logo search "..." --json` from within agent prompts.

## Project Structure

```
logosyncs/                        # リポジトリルート（実装はまだなし・設計フェーズ）
├── CLAUDE.md                     # このファイル（エージェント向けガイド）
├── SPEC.md                       # 詳細仕様書（JP）— CLI・DB・アーキテクチャ全容
├── .logosyncx/                   # logos CLI によるプラン・タスク管理
│   ├── config.json
│   ├── USAGE.md
│   └── templates/                # plan / task / knowledge / walkthrough テンプレート
└── mem/                          # 本ツールの設計根拠となった論文
    ├── mem.md                    # 論文原文（英語）: arXiv:2603.13017
    ├── mem_ja.md                 # 論文日本語訳
    ├── mem_meta.json             # 論文メタデータ
    └── _page_*.jpeg              # 論文中の図（PNG キャプチャ）
```

> **注**: 実装コードはまだ存在しない。詳細設計は `SPEC.md` を参照。
> `mem/mem.md` は本ツールの蒸留アルゴリズム・評価手法の出典論文（Sydney Lewis, 2026）。

## Tech Stack

| Layer | Choice |
|-------|--------|
| CLI | Python (typer) + pipx distribution |
| DB | SQLite — single `memory.db` with FTS5 (BM25) + sqlite-vec (ANN) + regular tables |
| Embeddings | `sentence-transformers` — `multilingual-e5-small` (384-dim, CPU, bilingual JP+EN) |
| Distillation LLM | `claude -p` (headless mode, no API key required) with `--output-format json --json-schema` |
| Symbol resolution | `tree-sitter` (Python bindings) — Python / TypeScript / Go |
| Automation | Claude Code Stop hook |

## Architecture

### Data Flow

```
.jsonl (Claude Code session logs)
  └── logo index
        ├── Split into exchanges (user turn + agent response = 1 exchange)
        ├── Embed with multilingual-e5-small → exchanges table (verbatim path)
        └── Queue for distillation

logo distill (background, nohup)
  ├── claude -p → palace object (exchange_core, specific_context, tags, files_touched)
  ├── Embed distill_text → palace_objects table
  └── tree-sitter on files_touched → symbols table
```

### Search (2-way cross-layer fusion)

```
query → BM25 (FTS5 verbatim) + HNSW (distilled embedding)
      → CombMNZ fusion → exchange_id → verbatim ref + symbols
```

論文ベスト構成: Cross BM25(V)+HNSW(D) / CombMNZ → MRR 0.759
HNSW(verbatim) は含めない（verbatim 長文は embedding 品質低、論文評価で有意改善なし）

### Storage

All data lives in `<project-root>/.logosyncs/memory.db`. Project scope is determined by git root (recorded at `logo init` time).

### Key Tables

- `exchanges` — raw user/agent content + embedding (verbatim path)
- `palace_objects` — distilled summaries + embedding (distilled path)
- `symbols` — tree-sitter resolved symbol name, kind, file, line, signature
- `tags` — per-palace-object tags extracted during distillation
- `conversations` — source `.jsonl` path pointers (used for dedup)

## CLI Commands

```bash
logo init                                    # Initialize .logosyncs/ in project root
logo index                                   # Index new .jsonl files from ~/.claude/projects/
logo distill                                 # Distill queued exchanges via claude -p (background)
logo search "query" --json --limit 5         # Semantic search (agent-facing)
logo context --symbol "Foo.bar" --json       # Reverse lookup: code -> past conversations
logo context --file src/foo.py --line 45     # Reverse lookup by file+line
logo show "~/.claude/.../abc.jsonl:ply=42"   # Fetch verbatim exchange
logo status                                  # Show index state
```

## Implementation Phases

- **Phase 1**: `.jsonl` parsing → exchange split → embedding → `logo search` via HNSW
- **Phase 2**: `claude -p` distillation → palace objects → cross-layer search (BM25 + HNSW distilled)
- **Phase 3**: tree-sitter symbol resolution → `symbols` table → `logo context --symbol`
- **Phase 4**: Claude Code Stop hook automation + `logo status`

## Exchange Boundary Definition

An exchange = from a `role: "user"` entry up to (but not including) the next `role: "user"` entry. Tool calls, intermediate responses, and multi-turn agent steps are all part of the same exchange.

## Design Notes

- Symbol resolution happens **at distill time**, not at search time — fast search, records survive file moves
- `logo distill` is launched detached (`nohup ... &`) so it outlives the Claude Code session
- No daemon required — automation via Stop hook only
- Team sharing is out of scope (SQLite binary + local paths); personal tool only
- Embedding model load (~500MB) is a known latency concern for `logo search` — to be addressed during implementation

## Logosyncx

Use `logos` CLI for plan and task management.
Full reference: `.logosyncx/USAGE.md`

**MANDATORY triggers:**

- **Start of every session** → `logos ls --json` (check past plans before doing anything)
- User says "save this plan" / "記録して" → `logos save --topic "..."` then write body with Write tool
- User says "make that a task" / "タスクにして" → `logos task create --plan <name> --title "..."`
- User says "continue from last time" / "前回の続き" → `logos ls --json` then `logos refer --name <name> --summary`

Always read the template before writing any document body. Write bodies directly into the file using the Write tool.
