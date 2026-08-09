"""loci prime — SessionStart Hook でエージェントのコンテキストにインストラクションを注入する"""

from __future__ import annotations

from pathlib import Path

import typer

BEGIN_MARKER = "<!-- BEGIN CODEATRIUM -->"
END_MARKER = "<!-- END CODEATRIUM -->"

PRIME_TEXT = """\
## Past Memory Search (codeatrium)

codeatrium records every past conversation turn, decision, and code location. Retrieve that memory **before** acting — not after.

### Context — recall from code (primary)

**This is the main way to use this tool.** Touching a function, component, or file = recalling memory about it. Before changing any code, look up what was decided about it.

```bash
# U1: file + symbol (primary form) — a function or component you're about to touch
loci context src/codeatrium/search.py:search_combined --json
loci context src/components/Button.tsx:Button --json

# U2: file only — before touching a file you don't know the history of
loci context src/codeatrium/search.py --json
```

If there's no exact hit, codeatrium widens the search itself (same file → same directory → semantic) and returns an honest confidence score for each result rather than guessing silently — you don't need a separate fallback query.

### When to act (agent-initiated triggers)

- **Before editing or refactoring a function or component** — recall past design decisions and known constraints for that symbol.
- **Before starting a new implementation** — check if similar work was done before; reuse decisions and avoid re-debating settled choices.
- **When you encounter a known or recurring error** — search for past fixes; the solution may already be documented.
- **When asked about work on a specific branch** — recall what was done and discussed on that branch.

```bash
# Retrieve past conversations from work on a specific branch
loci context --branch "feature/foo" --json
```

### IDE selection as a deictic anchor

When the IDE injects an active editor selection (shown as `⧉ Selected N lines from <file>`), treat that selection as the referent of "this / これ / この〜" in the user's prompt. The selection resolves *which* code the memory lookup is about — it is NOT by itself a request to recall.

Recall from a selection ONLY when BOTH hold:

1. a selection is active, AND
2. either (a) the user asks about the past — recall / why / history / decisions ("この実装の時の会話を思い出して", "これ前にどう決めた?"), or (b) your own next action on the selected code (edit / refactor / debug) needs prior decisions or constraints.

Do NOT recall when the selection is present only because the user is about to edit it and asks nothing past-oriented.

Pass the selection straight through — you do not need to find the enclosing function yourself first, codeatrium resolves it for you:

```bash
loci context <file>:<line> --json
```

- Selection IS a named function/class → look it up directly with its name instead of a line number: `loci context <file>:<name> --json`
- Selection is a fragment INSIDE one function/class → just pass the line number as above; no need to locate the `def`/`class` line yourself
- Selection spans multiple symbols or belongs to none (module-level code, config, comments), OR the lookups above return no results → fall back to semantic search over your question:

```bash
loci search "<the user's question>" --json
```

### Search — semantic query over past conversations (secondary)

Use this when you don't know which file or symbol to look at — you're searching for a concept, a design decision, or the reasoning behind something, not a specific piece of code.

```bash
# Find past discussions, decisions, or implementations
loci search "BM25 RRF fusion ranking" --json --limit 5

# Retrieve verbatim exchange (use verbatim_ref from search results)
loci show "<verbatim_ref>" --json
```\
"""

CLAUDE_MD_SECTION = f"""\
{BEGIN_MARKER}
## Past Memory Search (codeatrium)

IMPORTANT: Full usage instructions are injected automatically at session start via `loci prime` (SessionStart hook).
If not in context, run `loci prime`.
{END_MARKER}\
"""


def prime() -> None:
    """エージェント向けインストラクションを stdout に出力する。

    SessionStart Hook で自動実行され、エージェントのコンテキストウィンドウに
    使い方を注入する。CLAUDE.md にテンプレートを貼る必要がなくなる。

    未初期化プロジェクト（.codeatrium/ なし）では hook を無音で抜ける。
    エージェントのコンテキストや stderr を汚さないため。
    """
    from codeatrium.paths import CODEATRIUM_DIR, find_project_root

    root = find_project_root()
    if not (root / CODEATRIUM_DIR).exists():
        return
    typer.echo(PRIME_TEXT)


def inject_claude_md(project_root: Path) -> bool:
    """CLAUDE.md にマーカー付きセクションを挿入・更新する。

    Returns: True if file was modified.
    """
    claude_md = project_root / "CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text()
        if BEGIN_MARKER in content:
            # マーカー内を更新
            before = content[: content.index(BEGIN_MARKER)]
            after = content[content.index(END_MARKER) + len(END_MARKER) :]
            new_content = before + CLAUDE_MD_SECTION + after
            if new_content == content:
                return False
            claude_md.write_text(new_content)
            return True
        else:
            # 末尾に追加
            claude_md.write_text(content.rstrip() + "\n\n" + CLAUDE_MD_SECTION + "\n")
            return True
    else:
        claude_md.write_text("# CLAUDE.md\n\n" + CLAUDE_MD_SECTION + "\n")
        return True
