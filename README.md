<p align="center">
  <img src="assets/banner.svg" alt="codeatrium — memory palace for AI coding agents" width="720">
</p>

<h1 align="center">Codeatrium</h1>

<p align="center">
  <a href="https://github.com/senna-lang/Codeatrium/actions/workflows/ci.yml"><img src="https://github.com/senna-lang/Codeatrium/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/codeatrium/"><img src="https://img.shields.io/pypi/v/codeatrium" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

<p align="center">A <strong>memory palace</strong> for AI coding agents.</p>

<p align="center">English · <a href="README.ja.md">日本語</a></p>

<p align="center">
  <img src="assets/demo-search.svg" alt="loci search recalling a past design decision with its symbol, file:line, and git branch" width="640">
</p>

Codeatrium distills past conversations into *palace objects* and stores them in a searchable index, giving agents long-term memory. Past decisions, implementations, and code locations can be recalled in under 0.2 seconds.

The CLI command `loci` (from [Method of Loci](https://en.wikipedia.org/wiki/Method_of_loci)) is designed to be **called by the agent itself** — running `loci search "..." --json` from within a prompt.

The architecture extends the conversational memory model from [arXiv:2603.13017](https://arxiv.org/abs/2603.13017) for coding agents.

> **Note:** Currently [Claude Code](https://docs.anthropic.com/en/docs/claude-code) only. Session log format (`.jsonl`) and distillation (`claude --print`) depend on Claude Code.

## Simple Interface

Agents use these core commands:

- **Semantic search** — `loci search "query"` retrieves past conversations by semantic similarity
- **Reverse lookup from code** — `loci context --symbol "name"` recalls past conversations about a specific code symbol
  - tree-sitter symbol resolution (Python / TypeScript / Go) lets agents understand implementation intent before editing
- **Reverse lookup from branch** — `loci context --branch "name"` (or `loci search "query" --branch "name"`) recalls what was done and discussed on a specific git branch

Touching a symbol means recalling what was decided about it — `loci context` reverse-looks-up the exact code location, signature, and the conversation behind it:

<p align="center">
  <img src="assets/demo-context.svg" alt="loci context reverse-looking-up a symbol to the conversation that shaped it" width="640">
</p>

## How It Works

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
# Initialize in project root (also registers Claude Code hooks)
loci init
```

`loci init` now handles everything in one step — database setup, existing session detection, and Claude Code hook registration. Pass `--no-hooks` to skip hook registration. If init fails partway through, `.codeatrium/` is cleaned up automatically so re-running is safe.

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

Invalid input on any prompt re-prompts instead of silently falling back to a default.

## Agent Instructions

Agent instructions are injected automatically — no manual setup required:

- **`loci init`** — Inserts a marker section (`<!-- BEGIN CODEATRIUM -->...<!-- END CODEATRIUM -->`) into `CLAUDE.md`
- **`loci prime`** — Dynamically injects command usage into the context window at every session start via SessionStart Hook

## CLI Commands

| Command | Description |
|---------|-------------|
| `loci init` | Initialize `.codeatrium/` and register Claude Code hooks (`--no-hooks` to skip) |
| `loci index` | Index new session logs |
| `loci distill [--limit N]` | Distill undistilled exchanges via LLM |
| `loci search "query" --json` | Semantic search (agent-facing); add `--branch NAME` to filter by git branch |
| `loci context --symbol "name" --json` | Code symbol → past conversations (lightweight; add `--full` for verbatim text) |
| `loci context --branch "name" --json` | Git branch → past conversations (includes undistilled exchanges) |
| `loci show "<ref>" --json` | Retrieve verbatim conversation |
| `loci status` | Show index state |
| `loci prime` | Inject command usage into the session context (run automatically by the SessionStart hook) |
| `loci server start/stop/status` | Embedding server management |
| `loci hook install` | Re-register hooks (normally already done by `loci init`) |
| `loci hook uninstall` | Remove codeatrium hooks from `settings.json` |

## Automation (Claude Code Hooks)

After `loci init` (or `loci hook install`), everything runs automatically:

| Hook | Trigger | Command |
|------|---------|---------|
| Stop (async) | After every turn | `loci index` |
| SessionStart | startup / `/clear` / `/resume` / `compact` | `loci prime` |
| SessionStart | startup / `/clear` / `/resume` / `compact` | `loci server start` |
| SessionStart | startup / `/clear` / `/resume` / `compact` | `loci distill` |

- **`loci index`** — Runs asynchronously after every turn. Indexes only new exchanges, so it's fast even mid-session
- **`loci distill`** — Distills undistilled exchanges at session start via `claude --print`. Calls Haiku through the user's Claude Code (default: `claude-haiku-4-5`)
- **`loci server start`** — Keeps the embedding model (~500MB) resident in memory for sub-0.2s search latency

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

## Acknowledgments

The palace object model, room-based topic grouping, and BM25+HNSW fusion search are based on:

> *Structured Distillation for Personalized Agent Memory*
> (arXiv:2603.13017)


## License

MIT
