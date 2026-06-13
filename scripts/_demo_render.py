"""Render a realistic `loci` terminal session to stdout (ANSI).

Piped into Freeze by scripts/generate-demo.sh to produce the README demo
SVGs. Usage: `_demo_render.py [search|context]`.

The JSON shown is a representative example matching the documented
output schema (English, with symbols + git_branch populated). The two
demos tell one story: `search` recalls a design decision and points at
the `rrf_fuse` symbol; `context` reverse-looks-up that same symbol.
"""

import sys

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

console = Console(soft_wrap=False, width=72)


def prompt(cmd: str, query: str, flags: str) -> None:
    line = Text()
    line.append("❯ ", style="bold #4a9eff")
    line.append(cmd + " ", style="bold #e6edf3")
    line.append(query, style="#7bb8ff")
    line.append(" " + flags, style="#8a8a8a")
    console.print(line)
    console.print()


def body(json_text: str) -> None:
    console.print(
        Syntax(json_text, "json", theme="github-dark", background_color="default")
    )


def footer(summary: str, elapsed: str) -> None:
    line = Text()
    line.append("✓ ", style="bold #3fb950")
    line.append(summary, style="#e6edf3")
    line.append("  ·  ", style="#8a8a8a")
    line.append(elapsed, style="#8a8a8a")
    console.print()
    console.print(line)


SEARCH = """\
[
  {
    "exchange_core": "Chose RRF over CombMNZ to avoid hit_count skew",
    "rooms": [
      { "room_type": "concept", "room_label": "Rank fusion" }
    ],
    "symbols": [
      { "name": "rrf_fuse", "file": "src/codeatrium/search.py",
        "line": 88, "signature": "def rrf_fuse(bm25, hnsw, k=60)" }
    ],
    "verbatim_ref": ".../session.jsonl:ply=204",
    "git_branch": "feature/search-fusion"
  }
]"""

CONTEXT = """\
[
  {
    "symbol_name": "rrf_fuse",
    "symbol_kind": "function",
    "file_path": "src/codeatrium/search.py",
    "signature": "def rrf_fuse(bm25, hnsw, k=60)",
    "line": 88,
    "exchange_core": "Chose RRF over CombMNZ to avoid hit_count skew",
    "verbatim_ref": ".../session.jsonl:ply=204",
    "git_branch": "feature/search-fusion"
  }
]"""


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "search"
    if which == "search":
        prompt("loci search", '"why did we pick RRF over CombMNZ?"', "--json")
        body(SEARCH)
        footer("1 result", "0.18s")
    elif which == "context":
        prompt("loci context --symbol", '"rrf_fuse"', "--json")
        body(CONTEXT)
        footer("1 match", "0.04s")
    else:
        raise SystemExit(f"unknown demo: {which!r} (expected search|context)")


if __name__ == "__main__":
    main()
