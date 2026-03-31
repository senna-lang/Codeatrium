# AGENTS.md — codeatrium Usage Guide for AI Agents

This file describes how AI coding agents (Claude Code, Codex, etc.) should use
`loci` — the CLI memory tool for this project.

---

## What is codeatrium?

`loci` indexes past conversations between you and the user, distills them into
searchable summaries, and lets you retrieve past decisions, implementations, and
code locations in under 1 second.

**Primary user is you, the agent.** Call `loci search` proactively whenever you
need context about past work.

---

## When to Use

| Situation | Command |
|-----------|---------|
| "Where did we implement X?" | `loci search "X" --json --limit 5` |
| "Did we already fix this bug?" | `loci search "bug description" --json` |
| "Why did we choose this approach?" | `loci search "design decision keyword" --json` |
| About to edit a function | `loci context --symbol "FunctionName" --json` |
| Need verbatim conversation | `loci show "<verbatim_ref>" --json` |

---

## Commands

### Search past conversations

```bash
loci search "query" --json --limit 5
```

Response:
```json
[
  {
    "exchange_core": "1-2 sentence summary of what was done/decided",
    "specific_context": "exact technical detail: number, error message, param name, or file path",
    "rooms": [
      { "room_type": "concept|file|workflow", "room_key": "identifier", "room_label": "short label", "relevance": 0.9 }
    ],
    "symbols": [
      { "name": "SymbolName", "file": "src/...", "line": 42, "signature": "def ..." }
    ],
    "verbatim_ref": "~/.claude/projects/.../session.jsonl:ply=42"
  }
]
```

- `exchange_core` — what happened, using the exact terminology from the conversation
- `specific_context` — one precise detail copied verbatim from the conversation
- `symbols` — code symbols touched in that exchange (tree-sitter resolved)
- `verbatim_ref` — pointer to the full raw conversation

### Get full conversation text

```bash
loci show "~/.claude/projects/.../session.jsonl:ply=42" --json
```

Use this when `exchange_core` is not enough and you need the full context.

### Reverse lookup: code → past conversations

Before editing a symbol, check past discussions about it:

```bash
loci context --symbol "SymbolName" --json
```

### Check index status

```bash
loci status
```

---

## Interpreting Results

- Results with `exchange_core: null` are indexed but not yet distilled. Use
  `loci show` with the `verbatim_ref` to read the raw conversation.
- `rooms` group related exchanges by topic. Use `room_key` values as search
  queries to find related conversations.
- `symbols` link past conversations to specific code locations. If a symbol
  appears, that file was modified during that conversation.

---

## Best Practices

1. **Search before implementing** — always check if something was discussed or
   built before starting work.
2. **Use technical terms** — queries with exact symbol names, error messages, or
   parameter names yield better results than natural language paraphrases.
3. **Follow up with `loci show`** — when `exchange_core` is ambiguous, fetch the
   full verbatim conversation.
4. **Check symbols before editing** — run `loci context --symbol` for any
   non-trivial function you are about to change.

---

## Infrastructure

The following runs automatically. You do not need to manage it.

| What | When | How |
|------|------|-----|
| `loci index` | After every agent turn | Stop hook (async) |
| `loci server start` | On session start / `/clear` / `/resume` | SessionStart hook |
| `loci distill` | On session start / `/clear` / `/resume` | SessionStart hook |

The embedding server stays resident in memory so `loci search` returns in
<0.2s after the first call.

---

## Limitations

- Only Claude Code `.jsonl` sessions for **this project** are indexed (scoped to
  git root).
- Conversations not yet distilled return `exchange_core: null`. Distillation
  runs in the background at session boundaries.
- Symbol lookup requires the file to still exist at the recorded path.