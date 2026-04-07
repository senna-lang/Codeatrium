"""loci prime — SessionStart Hook でエージェントのコンテキストにインストラクションを注入する"""

from __future__ import annotations

from pathlib import Path

import typer

BEGIN_MARKER = "<!-- BEGIN CODEATRIUM -->"
END_MARKER = "<!-- END CODEATRIUM -->"

PRIME_TEXT = """\
## Past Memory Search (codeatrium)

Use `loci search` to find past implementations, decisions, and code locations.

### When to use

- When asked "where did we implement X?" or "where is X?"
- When checking if a similar bug was fixed before
- When verifying if a feature already exists
- When looking up the reasoning behind a past design decision
- Before editing code you lack context about — use `loci context --symbol` to review past discussions
- Before refactoring or changing the behavior of a function — use `loci context --symbol` to check past design decisions

### Commands

```bash
# Semantic search
loci search "query" --json --limit 5

# Reverse lookup: code symbol -> past conversations
loci context --symbol "symbol_name" --json

# Retrieve verbatim conversation (use verbatim_ref from search results)
loci show "<verbatim_ref>" --json
```\
"""

CLAUDE_MD_SECTION = f"""\
{BEGIN_MARKER}
## Past Memory Search (codeatrium)

IMPORTANT: Command usage is injected automatically at session start via `loci prime` (SessionStart hook).
If not in context, run `loci prime`.

### Rules

1. **Search before implementing** — always check if something was discussed or built before starting work.
2. **Check symbols when you lack context** — run `loci context --symbol` before changing a function you don't have enough background on.
3. **Use technical terms** — queries with exact symbol names, error messages, or parameter names yield better results.
4. **Follow up with `loci show`** — when `exchange_core` is ambiguous, fetch the full verbatim conversation.
{END_MARKER}\
"""


def prime() -> None:
    """エージェント向けインストラクションを stdout に出力する。

    SessionStart Hook で自動実行され、エージェントのコンテキストウィンドウに
    使い方を注入する。CLAUDE.md にテンプレートを貼る必要がなくなる。
    """
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
