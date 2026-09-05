<p align="center">
  <img src="assets/banner.svg" alt="codeatrium — two commands, recall everything: a minimal memory layer for AI coding agents" width="100%">
</p>

<p align="center">
  <a href="https://github.com/senna-lang/Codeatrium/actions/workflows/ci.yml"><img src="https://github.com/senna-lang/Codeatrium/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/codeatrium/"><img src="https://img.shields.io/pypi/v/codeatrium" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

<p align="center">English · <a href="README.ja.md">日本語</a></p>

<p align="center">
  <img src="assets/demo-search.svg" alt="loci search recalling a past design decision with its symbol, file:line, and git branch" width="640">
</p>

An AI coding agent recalls everything it has done through just two commands: `loci search` and `loci context`. That's the whole interface. The agent reaches for the right call without hesitation, and restores past decisions, conversations, and exact code locations in under 0.2 seconds.

The CLI command `loci` is designed to be **called by the agent itself** — running `loci search "..." --json` from within a prompt. *(The name comes from the [Method of Loci](https://en.wikipedia.org/wiki/Method_of_loci) — the memory-palace technique. Under the hood, conversations are distilled into "palace objects"; see [How It Works](#how-it-works). The architecture extends the conversational memory model from [arXiv:2603.13017](https://arxiv.org/abs/2603.13017) for coding agents.)*

> **Harnesses:** Claude Code, Codex CLI, Oh My Pi, OpenCode, and Grok session logs are indexed into the same exchange, code-touch, symbol, search, context, and `show` contracts. Distillation is independent of the harness and can use `claude --print` or a local OpenAI-compatible LLM.

## Minimal Interface

The whole recall interface is two commands:

- **`loci search "query"`** — semantic search over past conversations
- **`loci context`** — reverse lookup, by code symbol (`--symbol "name"`) or git branch (`--branch "name"`)
  - tree-sitter symbol resolution (Python / TypeScript / Go) lets agents understand implementation intent before editing
  - `--branch "name"` recalls what was done and discussed on a specific git branch (also available as `loci search "query" --branch "name"`)

That's deliberate. The user here is the agent, and an agent handed a 50-tool palette hesitates, mis-picks, and burns tokens just deciding which to call. With a surface this small — and no MCP tool schemas sitting resident in the context window — the agent reaches for the right call the first time, every time. *(When the full transcript is needed, `loci show "<exchange-id>"` expands a search result to its stored verbatim source.)*

Touching a symbol means recalling what was decided about it — `loci context` reverse-looks-up the exact code location, signature, and the conversation behind it:

<p align="center">
  <img src="assets/demo-context.svg" alt="loci context reverse-looking-up a symbol to the conversation that shaped it" width="640">
</p>

## How It Works

<p align="center">
  <img src="assets/how-it-works.svg" alt="session logs are indexed into exchanges, distilled into palace objects with symbols, then recalled via BM25 + HNSW fused by RRF" width="100%">
</p>

1. **Index** — Splits agent session logs into exchanges (user utterance + agent response pairs) and indexes them with FTS5 for keyword search
2. **Distill** — An LLM (`claude --print`, default `claude-haiku-4-5`) summarizes each exchange into a palace object: `exchange_core` (what was done), `specific_context` (concrete details), `room_assignments` (topic tags). tree-sitter resolves touched files to symbol level (function/class/method + file + line + signature)
3. **Search** — Cross-layer search fusing BM25 on verbatim text with HNSW on distilled embeddings via RRF

Raw conversations are not embedded — only the condensed distilled text is embedded with `multilingual-e5-small` (384-dim), balancing semantic search quality with embedding cost. The embedding model runs as a **Unix socket server**, keeping search latency **under 0.2 seconds** after the first load.

## Installation

```bash
pipx install codeatrium
```

Requires Python 3.11+.

## Quick Start

```bash
# Initialize in project root. This creates `.codeatrium/` and adds the shared
# agent reminder to AGENTS.md.
loci init
```

`loci init` creates the project-local database, writes the common `AGENTS.md` instruction section, and installs Claude Code hooks unless `--no-hooks` is supplied. Register Codex hooks explicitly with `loci hook install --harness codex`; unsupported native hooks print a complete fallback recipe. If init fails partway through, `.codeatrium/` is cleaned up automatically so re-running is safe.

When running `loci init`, if past session logs are detected, you'll be prompted with:

> [!IMPORTANT]
> When adopting this tool mid-project, a large number of exchanges may already exist. Distilling all of them will consume significant `claude --print` (Haiku) tokens. We recommend starting with `Skip all` or `Distill last 50`.

1. **Min chars threshold** — Minimum character filter applied at index time (default: 50). Shorter exchanges are skipped entirely, which also shrinks the pool of distillation candidates. Higher values exclude short conversations and reduce token usage; lower values include nearly everything. (Distillation applies a separate `min_chars` of 100 — see [Configuration](#configuration).)
2. **Handling existing exchanges** — Choose how much past history to distill:
   - Skip all (no past session distillation)
   - Distill last 50 (recent history only)
   - Distill all (everything — high token cost)
   - Custom (specify a number)
3. **Run distillation now?** — Accepts `1`/`2`/`y`/`n`/`yes`/`no`. Choose No to defer to the next session start.

`loci init` also asks once, regardless of past session history:

4. **Use a local distillation model?** — Offers [`qwen2.5-7b-memory-distiller`](https://huggingface.co/sennaLLMLearner/qwen2.5-7b-memory-distiller), a Qwen2.5-7B fine-tuned specifically for this distillation task (SFT + ORPO on WildChat-1M). Choosing Yes runs `ollama pull hf.co/sennaLLMLearner/qwen2.5-7b-memory-distiller:Q4_K_M` (~4.7GB, requires [Ollama](https://ollama.com)) and writes `provider = "openai"` into `config.toml` pointing at it. Choosing No (default) keeps `claude --print` (Haiku). If Ollama isn't installed or the pull fails, init warns and falls back to Haiku automatically. Pass `--no-local-distiller` to skip this prompt entirely.

Invalid input on any prompt re-prompts instead of silently falling back to a default.

## Agent Instructions

`loci init` installs the marker section (`<!-- BEGIN CODEATRIUM -->...<!-- END CODEATRIUM -->`) in **`AGENTS.md`**, the common instruction source for every supported harness. `loci prime` injects full command usage into a session context when native lifecycle support is available.

## CLI Commands

| Command | Description |
|---------|-------------|
| `loci init` | Initialize `.codeatrium/`, write common `AGENTS.md` instructions, and install Claude hooks (`--no-hooks` to skip, `--no-local-distiller` to skip the local-model prompt) |
| `loci index [--harness all\|claude\|codex\|opencode\|omp-pi\|grok]` | Index new session logs; the default indexes every detected harness |
| `loci distill [--limit N]` | Distill undistilled exchanges via LLM |
| `loci search "query" --json` | Semantic search (agent-facing); add `--branch NAME` to filter by git branch |
| `loci context --symbol "name" --json` | Code symbol → past conversations (lightweight; add `--full` for verbatim text) |
| `loci context --branch "name" --json` | Git branch → past conversations (includes undistilled exchanges) |
| `loci show "<exchange-id>" --json` | Retrieve a stored exchange by its primary ID |
| `loci status` | Show index state |
| `loci prime` | Inject command usage into the session context |
| `loci server start/stop/status` | Embedding server management |
| `loci hook install --harness NAME` | Install native lifecycle hooks or print the harness fallback recipe |
| `loci hook uninstall --harness NAME` | Remove native codeatrium lifecycle hooks |

## Harness Lifecycle

| Harness | Transcript source | Native lifecycle | Fallback |
|---------|-------------------|------------------|----------|
| Claude Code | Project JSONL | `~/.claude/settings.json` | — |
| Codex CLI | Global rollout JSONL filtered by recorded cwd | `~/.codex/hooks.json` | Compact runs `loci prime` through SessionStart |
| Oh My Pi | Project JSONL | — | Index after each turn; run server, distill, and prime at session start |
| OpenCode | Local session SQLite | — | Index after each turn; run server, distill, and prime at session start |
| Grok | Project streaming JSONL | — | Index after each turn; run server, distill, and prime at session start |

Native hooks map turn end to `loci index`, session start to `loci server start`, `loci distill`, and `loci prime`, and compact to `loci prime`. Fallback recipes are emitted by `loci hook install --harness NAME` and never modify Claude settings.

## Search Output

```json
[
  {
    "exchange_core": "Added connection pool with pool_size=5",
    "specific_context": "pool_size=5, max_overflow=10",
    "rooms": [
      { "room_type": "concept", "room_key": "db-pool", "room_label": "DB connection pooling" }
    ],
    "symbols": [
      { "name": "create_pool", "file": "src/db.py", "line": 42, "signature": "def create_pool(...)" }
    ],
    "verbatim_ref": "~/.claude/projects/.../session.jsonl:ply=42",
    "git_branch": "feature/db-pool"
  }
]
```

## Configuration

`.codeatrium/config.toml` (generated by `loci init`):

```toml
[distill]
provider = "claude"                    # Distillation backend: "claude" | "openai" (default "claude")
model = "claude-haiku-4-5"             # Model for distillation (default)
batch_limit = 20                       # Max distillations per hook run
min_chars = 100                        # Skip distillation for exchanges shorter than this

[index]
min_chars = 50                         # Skip indexing exchanges shorter than this
```

There are two `min_chars` settings: `[index] min_chars` controls what gets indexed at all, while `[distill] min_chars` further skips distillation (the LLM cost) for short exchanges that were already indexed.

### Distilling with a local LLM

Distillation is a small per-exchange structured-extraction task, so a local model is usually good enough. Any OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp-server, vLLM) works by setting `provider = "openai"` and `base_url` — no new dependencies, no API key (the `Authorization` header is never sent, so this is local-only):

```toml
[distill]
provider = "openai"
model = "qwen2.5:7b"
base_url = "http://localhost:11434/v1"   # Ollama
# base_url = "http://localhost:1234/v1"  # LM Studio
```

`base_url` is required when `provider = "openai"`; if it is missing or empty the provider falls back to `claude` with a warning. With `provider = "claude"` (the default), `base_url` is ignored and distillation runs through `claude --print` as before.

`loci init` offers to set this up for you automatically with [`qwen2.5-7b-memory-distiller`](https://huggingface.co/sennaLLMLearner/qwen2.5-7b-memory-distiller), a model fine-tuned specifically for this task (see the prompt above) — no manual config needed if you accept it.

## Acknowledgments

The palace object model, room-based topic grouping, and BM25+HNSW fusion search are based on:

> *Structured Distillation for Personalized Agent Memory*
> (arXiv:2603.13017)


## License

MIT
