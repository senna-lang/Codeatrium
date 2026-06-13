"""loci prime — SessionStart Hook でエージェントのコンテキストにインストラクションを注入する"""

from __future__ import annotations

from pathlib import Path

import typer

BEGIN_MARKER = "<!-- BEGIN CODEATRIUM -->"
END_MARKER = "<!-- END CODEATRIUM -->"

PRIME_TEXT = """\
## Past Memory Search (codeatrium)

codeatrium records every past conversation turn, decision, and code location. Retrieve that memory **before** acting — not after.

### When to act (agent-initiated triggers)

- **Before editing or refactoring a function** — recall past design decisions and known constraints for that symbol.
- **Before starting a new implementation** — check if similar work was done before; reuse decisions and avoid re-debating settled choices.
- **When you encounter a known or recurring error** — search for past fixes; the solution may already be documented.
- **When asked about work on a specific branch** — recall what was done and discussed on that branch.

### Search — semantic query over past conversations

```bash
# Find past discussions, decisions, or implementations
loci search "BM25 RRF fusion ranking" --json --limit 5

# Retrieve verbatim exchange (use verbatim_ref from search results)
loci show "<verbatim_ref>" --json
```

### Context — reverse lookup from code symbol or git branch to past conversations

Touching a symbol = recalling memory about that symbol. Before changing any function or class, look up what was decided about it.

```bash
# Retrieve all past conversations that involved this symbol
loci context --symbol "SymbolResolver.extract" --json

# Retrieve past conversations from work on a specific branch
loci context --branch "feature/foo" --json
```

### IDE selection as a deictic anchor

When the IDE injects an active editor selection (shown as `⧉ Selected N lines from <file>`), treat that selection as the referent of "this / これ / この〜" in the user's prompt. The selection resolves *which* symbol the memory lookup is about — it is NOT by itself a request to recall.

Recall from a selection ONLY when BOTH hold:

1. a selection is active, AND
2. either (a) the user asks about the past — recall / why / history / decisions ("この実装の時の会話を思い出して", "これ前にどう決めた?"), or (b) your own next action on the selected code (edit / refactor / debug) needs prior decisions or constraints.

Do NOT recall when the selection is present only because the user is about to edit it and asks nothing past-oriented.

Map the selection to a query:

- Selection IS a named function/class → look it up directly:

```bash
loci context --symbol "<name>" --json
```

- Selection is a fragment INSIDE one function/class — the `def`/`class` line is usually not in the selection, so resolve the enclosing symbol first (LSP `workspaceSymbol`/`hover`, or read `<file>` around the selection), then:

```bash
loci context --symbol "<enclosing-symbol>" --json
```

- Selection spans multiple symbols or belongs to none (module-level code, config, comments), OR the lookups above return no results → fall back to semantic search over your question:

```bash
loci search "<the user's question>" --json
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
