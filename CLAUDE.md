# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`logosyncs` is a CLI-first memory layer for AI coding agents. The command is `logo`. It lets agents like Claude Code search past conversations, retrieve code locations (file + line + symbol), and link conversation history to code symbols.

Primary user is **the agent itself**, not a human. The tool is invoked via `logo search "..." --json` from within agent prompts.

## Project Structure

```
logosyncs/
├── CLAUDE.md                     # このファイル（エージェント向けガイド）
├── AGENTS.md                     # 他エージェント向け使用ガイド（共通）
├── SPEC.md                       # 詳細仕様書（JP）
├── REFACTOR.md                   # リファクタリング計画
├── src/logo/                     # 実装コード
│   ├── cli.py                    # CLI エントリポイント（typer）
│   ├── indexer.py                # .jsonl パース・exchange 分割
│   ├── distiller.py              # claude -p 蒸留パイプライン
│   ├── search.py                 # BM25 + HNSW RRF 融合検索
│   ├── embedder.py               # multilingual-e5-small ラッパー
│   ├── embedder_server.py        # Unix ソケット embedding サーバー
│   ├── resolver.py               # tree-sitter シンボル解決
│   └── db.py                     # SQLite スキーマ・接続管理
├── tests/                        # pytest テスト（84件）
├── .logosyncs/memory.db          # インデックス DB（git 管理外）
└── .logosyncx/                   # logos CLI によるプラン・タスク管理
```

## Tech Stack

| Layer | Choice |
|-------|--------|
| CLI | Python (typer) + pipx distribution |
| DB | SQLite — single `memory.db` with FTS5 (BM25) + sqlite-vec (ANN) |
| Embeddings | `sentence-transformers` — `multilingual-e5-small` (384-dim, CPU, bilingual JP+EN) |
| Embedding server | Unix socket server（常駐・アイドル10分で自動停止） |
| Distillation LLM | `claude --print` (headless, OAuth, no API key) |
| Symbol resolution | `tree-sitter` — Python / TypeScript / Go |
| Automation | Claude Code SessionStart / Stop hook |

## Architecture

### Data Flow

```
.jsonl (Claude Code session logs)
  └── logo index  [Stop hook: async, every turn]
        ├── Split into exchanges
        ├── Embed with multilingual-e5-small → exchanges table
        └── Queue for distillation

logo distill  [SessionStart hook: on startup / /clear / /resume]
  ├── claude --print → palace object (exchange_core, specific_context, room_assignments)
  ├── Embed distill_text → palace_objects table
  └── tree-sitter on files_touched → symbols table

logo server start  [SessionStart hook: warming up embedding server]
  └── Unix socket server keeps model in memory → logo search <1s
```

### Search (cross-layer RRF fusion)

```
query → BM25 (FTS5 verbatim) + HNSW (distilled embedding)
      → RRF fusion → exchange_id → verbatim ref + symbols
```

論文ベスト構成: Cross BM25(V)+HNSW(D) / RRF → MRR 0.759

## CLI Commands

```bash
logo init                                    # Initialize .logosyncs/ in project root
logo index                                   # Index new .jsonl files
logo distill [--limit N]                     # Distill queued exchanges via claude --print
logo search "query" --json --limit 5         # Semantic search (agent-facing)
logo context --symbol "Foo.bar" --json       # Reverse lookup: code -> past conversations
logo show "~/.claude/.../abc.jsonl:ply=42"   # Fetch verbatim exchange
logo status                                  # Show index state
logo server start / stop / status            # Embedding server management
logo hook install                            # Register hooks to ~/.claude/settings.json
```

## Hooks (registered in ~/.claude/settings.json)

| Hook | Trigger | Command |
|------|---------|---------|
| Stop (async) | 毎ラリー後 | `logo index` |
| SessionStart | startup / clear / resume / compact | `logo server start` |
| SessionStart | startup / clear / resume / compact | `logo distill` |

---

## Past Memory Search (logosyncs)

過去の実装・意思決定・コードの位置を検索するには `logo search` を使う。

### いつ使うか

- 「〜はどこに実装した？」「〜ってどこだっけ？」と聞かれたとき
- 過去に同じバグを直したか確認したいとき
- ある機能がすでに実装済みか調べたいとき
- 実装の意思決定の理由を確認したいとき
- コードを編集する前に、そのシンボルに関する過去の議論を確認したいとき

### 検索

```bash
logo search "クエリ" --json --limit 5
```

出力:
```json
[
  {
    "exchange_core": "何をしたかの要約（1-2文）",
    "specific_context": "技術的詳細（数値・エラーメッセージ・パラメータ名）",
    "rooms": [
      { "room_type": "concept", "room_key": "rrf-adoption", "room_label": "RRF adoption reason" }
    ],
    "symbols": [
      { "name": "distill", "file": "src/logo/cli.py", "line": 161, "signature": "def distill(...)" }
    ],
    "verbatim_ref": "~/.claude/projects/.../abc.jsonl:ply=42"
  }
]
```

### 原文の取得

検索結果の `verbatim_ref` を使って会話の原文を取得:

```bash
logo show "~/.claude/projects/.../abc.jsonl:ply=42" --json
```

### コードから逆引き

編集しようとしているシンボルに関する過去の会話を確認:

```bash
logo context --symbol "distill" --json
```

---

## Design Notes

- RRF (Reciprocal Rank Fusion) を採用。CombMNZ は hit_count 問題があるため不採用
- `logo distill` の `claude --print` 呼び出しには `--no-session-persistence --setting-sources ""` を付ける（CLAUDE.md 非読込・27K token 問題を回避）
- Embedding server は Unix socket 常駐。2回目以降の `logo search` は <0.2秒
- コンパクション要約（"This session is being continued..."）は exchange 境界として扱わない

## Logosyncx

Use `logos` CLI for plan and task management.
Full reference: `.logosyncx/USAGE.md`

**MANDATORY triggers:**

- **Start of every session** → `logos ls --json` (check past plans before doing anything)
- User says "save this plan" / "記録して" → `logos save --topic "..."` then write body with Write tool
- User says "make that a task" / "タスクにして" → `logos task create --plan <name> --title "..."`
- User says "continue from last time" / "前回の続き" → `logos ls --json` then `logos refer --name <name> --summary`

Always read the template before writing any document body. Write bodies directly into the file using the Write tool.