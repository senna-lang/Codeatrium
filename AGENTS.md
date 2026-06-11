# codeatrium — Agent Usage Guide

`codeatrium` is a CLI-first memory layer for AI coding agents. The command is `loci`. It lets agents search past conversations, retrieve code locations (file + line + symbol), and link conversation history to code symbols.

Primary user is **the agent itself**, not a human. The tool is invoked via `loci search "..." --json` from within agent prompts.

## When to use

- When asked "where did we implement X?" or "where is X?"
- When checking if a similar bug was fixed before
- When verifying if a feature already exists
- When looking up the reasoning behind a past design decision
- Before editing code you lack context about — use `loci context --symbol` to review past discussions
- Before refactoring or changing the behavior of a function — use `loci context --symbol` to check past design decisions
- When recalling work done on a specific branch — use `loci context --branch` to find past conversations

## CLI Commands

```bash
loci init                                    # Initialize .codeatrium/ in project root
loci index                                   # Index new .jsonl files
loci distill [--limit N]                     # Distill queued exchanges via claude --print
loci search "query" --json --limit 5         # Semantic search (agent-facing)
loci search "query" --branch NAME --json     # Branch-filtered semantic search
loci context --symbol "Foo.bar" --json       # Reverse lookup: code -> past conversations (lightweight; use loci show <verbatim_ref> for full text)
loci context --branch NAME --json            # Branch reverse lookup (undistilled exchanges included)
loci show "~/.claude/.../abc.jsonl:ply=42"   # Fetch verbatim exchange
loci status                                  # Show index state
loci server start / stop / status            # Embedding server management
loci hook install                            # Register hooks to ~/.claude/settings.json
```
