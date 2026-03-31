# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`codeatrium` is a CLI-first memory layer for AI coding agents. The command is `loci`. It lets agents like Claude Code search past conversations, retrieve code locations (file + line + symbol), and link conversation history to code symbols.

Primary user is **the agent itself**, not a human. The tool is invoked via `loci search "..." --json` from within agent prompts.

## Project Structure

```
codeatrium/
├── CLAUDE.md                          # このファイル（エージェント向けガイド）
├── AGENTS.md                          # 他エージェント向け使用ガイド（共通）
├── src/codeatrium/
│   ├── cli/                           # CLI 層（typer）
│   │   ├── __init__.py                # app 定義 + init + サブコマンド登録
│   │   ├── index_cmd.py               # loci index
│   │   ├── distill_cmd.py             # loci distill
│   │   ├── search_cmd.py              # loci search / context
│   │   ├── show_cmd.py                # loci show / dump
│   │   ├── status_cmd.py              # loci status
│   │   ├── hook_cmd.py                # loci hook install
│   │   └── server_cmd.py              # loci server start/stop/status
│   ├── models.py                      # 共有データクラス
│   ├── paths.py                       # パス解決ヘルパー
│   ├── config.py                      # .codeatrium/config.toml 読み込み
│   ├── hooks.py                       # Claude Code hook JSON 操作
│   ├── llm.py                         # claude --print ラッパー + プロンプト
│   ├── db.py                          # SQLite スキーマ・接続管理
│   ├── indexer.py                     # .jsonl パース・exchange 分割
│   ├── distiller.py                   # 蒸留パイプライン
│   ├── search.py                      # BM25 + HNSW RRF 融合検索
│   ├── embedder.py                    # multilingual-e5-small ラッパー
│   ├── embedder_server.py             # Unix ソケット embedding サーバー
│   └── resolver.py                    # tree-sitter シンボル解決
├── tests/                             # pytest テスト（96件）
├── .codeatrium/                       # インデックス DB + config（git 管理外）
│   ├── memory.db
│   └── config.toml
└── docs/internal/                     # 内部ドキュメント（git 管理外）
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
  └── loci index  [Stop hook: async, every turn]
        ├── Split into exchanges
        ├── Embed with multilingual-e5-small → exchanges table
        └── Queue for distillation

loci distill  [SessionStart hook: on startup / /clear / /resume]
  ├── claude --print → palace object (exchange_core, specific_context, room_assignments)
  ├── Embed distill_text → palace_objects table
  └── tree-sitter on files_touched → symbols table

loci server start  [SessionStart hook: warming up embedding server]
  └── Unix socket server keeps model in memory → loci search <1s
```

### Search (cross-layer RRF fusion)

```
query → BM25 (FTS5 verbatim) + HNSW (distilled embedding)
      → RRF fusion → exchange_id → verbatim ref + symbols
```

論文ベスト構成: Cross BM25(V)+HNSW(D) / RRF → MRR 0.759

## Configuration

`.codeatrium/config.toml` でカスタマイズ可能（`loci init` 時に自動生成）:

```toml
[distill]
model = "claude-haiku-4-5-20251001"   # 蒸留に使うモデル
batch_limit = 20                       # hook 1回あたりの蒸留上限
```

## CLI Commands

```bash
loci init                                    # Initialize .codeatrium/ in project root
loci index                                   # Index new .jsonl files
loci distill [--limit N]                     # Distill queued exchanges via claude --print
loci search "query" --json --limit 5         # Semantic search (agent-facing)
loci context --symbol "Foo.bar" --json       # Reverse lookup: code -> past conversations
loci show "~/.claude/.../abc.jsonl:ply=42"   # Fetch verbatim exchange
loci status                                  # Show index state
loci server start / stop / status            # Embedding server management
loci hook install                            # Register hooks to ~/.claude/settings.json
```

## Hooks (registered in ~/.claude/settings.json)

| Hook | Trigger | Command |
|------|---------|---------|
| Stop (async) | 毎ラリー後 | `loci index` |
| SessionStart | startup / clear / resume / compact | `loci server start` |
| SessionStart | startup / clear / resume / compact | `loci distill --limit <batch_limit>` |

---

## Past Memory Search (codeatrium)

過去の実装・意思決定・コードの位置を検索するには `loci search` を使う。

### いつ使うか

- 「〜はどこに実装した？」「〜ってどこだっけ？」と聞かれたとき
- 過去に同じバグを直したか確認したいとき
- ある機能がすでに実装済みか調べたいとき
- 実装の意思決定の理由を確認したいとき
- コードを編集する前に、そのシンボルに関する過去の議論を確認したいとき

### 検索

```bash
loci search "クエリ" --json --limit 5
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
      { "name": "distill", "file": "src/codeatrium/distiller.py", "line": 55, "signature": "def distill_exchange(...)" }
    ],
    "verbatim_ref": "~/.claude/projects/.../abc.jsonl:ply=42"
  }
]
```

### 原文の取得

検索結果の `verbatim_ref` を使って会話の原文を取得:

```bash
loci show "~/.claude/projects/.../abc.jsonl:ply=42" --json
```

### コードから逆引き

編集しようとしているシンボルに関する過去の会話を確認:

```bash
loci context --symbol "distill" --json
```

---

## Design Notes

- RRF (Reciprocal Rank Fusion) を採用。CombMNZ は hit_count 問題があるため不採用
- `loci distill` の `claude --print` 呼び出しには `--no-session-persistence --setting-sources ""` を付ける（CLAUDE.md 非読込・27K token 問題を回避）
- Embedding server は Unix socket 常駐。2回目以降の `loci search` は <0.2秒
- コンパクション要約（"This session is being continued..."）は exchange 境界として扱わない
- `loci init` 時に既存 exchange の蒸留範囲を対話プロンプトで選択可能（トークン消費制御）

## Logosyncx

Use `logos` CLI for plan and task management.
Full reference: `.logosyncx/USAGE.md`

**MANDATORY triggers:**

- **Start of every session** → `logos ls --json` (check past plans before doing anything)
- User says "save this plan" / "記録して" → `logos save --topic "..."` then write body with Write tool
- User says "make that a task" / "タスクにして" → `logos task create --plan <name> --title "..."`
- User says "continue from last time" / "前回の続き" → `logos ls --json` then `logos refer --name <name> --summary`

Always read the template before writing any document body. Write bodies directly into the file using the Write tool.
