# codeatrium — Agent Usage Guide

`codeatrium` is a CLI-first memory layer for AI coding agents. The command is `loci`. It lets agents search past conversations, retrieve code locations (file + line + symbol), and link conversation history to code symbols.

Primary user is **the agent itself**, not a human. The main entry point is `loci context <file>[:<symbol-or-line>] --json` — before touching a function, component, or file, recall what's already known about it. `loci search "..." --json` is a secondary, word-based fallback for when you don't know which file or symbol to look at.

## When to use

- Before editing or refactoring a function or component — `loci context <file>:<symbol> --json` to review past discussions and design decisions
- Before touching a file you don't know the history of — `loci context <file> --json`
- With an IDE selection (`<file>:<line>`) — pass it straight through; codeatrium resolves the enclosing function itself, no need to locate it yourself
- When asked "where did we implement X?" or "where is X?", or checking if a similar bug was fixed before, or looking up the reasoning behind a past design decision, and you don't have a specific file/symbol yet — `loci search "..." --json`
- When recalling work done on a specific branch — use `loci context --branch` to find past conversations

## CLI Commands

```bash
loci init                                    # Initialize .codeatrium/ in project root
loci index [--harness all|claude|codex|opencode|omp-pi|grok]  # Index detected harness logs
loci distill [--limit N]                     # Distill queued exchanges via claude --print
loci context <file>:<symbol> --json          # U1: recall from a function/component (primary)
loci context <file> --json                   # U2: recall from a file
loci context <file>:<line> --json            # IDE selection: resolves the enclosing symbol itself
loci context --symbol "Foo.bar" --json       # Legacy form (no file scope, may collide across files); prefer <file>:<symbol>
loci context --branch NAME --json            # Branch reverse lookup (undistilled exchanges included)
loci search "query" --json --limit 5         # Semantic search (secondary, word-based)
loci search "query" --branch NAME --json     # Branch-filtered semantic search
loci show "<exchange-id>" --json                 # Fetch a stored exchange by ID
loci status                                  # Show index state
loci server start / stop / status            # Embedding server management
loci hook install [--harness NAME]                # Install Claude hooks or print fallback
loci eval gen --dataset symbol-recall             # Build the symbol-recall eval dataset from this repo's own corpus
loci eval run --dataset symbol-recall --adapter symbol --json  # Recall@k/MRR@10 for the code→conversation lookup (completeness, no baseline)
```
