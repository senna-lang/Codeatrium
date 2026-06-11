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

### Search — semantic query over past conversations

```bash
# Find past discussions, decisions, or implementations
loci search "BM25 RRF fusion ranking" --json --limit 5

# Retrieve verbatim exchange (use verbatim_ref from search results)
loci show "<verbatim_ref>" --json
```

### Context — reverse lookup from code symbol to past conversations

Touching a symbol = recalling memory about that symbol. Before changing any function or class, look up what was decided about it.

```bash
# Retrieve all past conversations that involved this symbol
loci context --symbol "SymbolResolver.extract" --json
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
