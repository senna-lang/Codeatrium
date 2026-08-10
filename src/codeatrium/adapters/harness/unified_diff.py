"""標準 unified diff を、ハーネス非依存の編集位置と変更量へ正規化する。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from codeatrium.models import LineRange

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@(?: .*)?$"
)


@dataclass(frozen=True)
class ParsedUnifiedDiff:
    """unified diff から直接読み取れる編集範囲と行ごとの変更量。"""

    line_ranges: tuple[LineRange, ...]
    added: int
    removed: int


def parse_unified_diff(diff: str) -> ParsedUnifiedDiff | None:
    """有効な hunk を含む diff を解析し、なければ ``None`` を返す。"""
    line_ranges: list[LineRange] = []
    added = 0
    removed = 0
    old_remaining = 0
    new_remaining = 0

    for line in diff.splitlines():
        header = _HUNK_HEADER_RE.match(line)
        if header is not None:
            old_remaining = int(header["old_lines"] or 1)
            new_remaining = int(header["new_lines"] or 1)
            line_ranges.append(
                LineRange(
                    old_start=int(header["old_start"]),
                    old_lines=old_remaining,
                    new_start=int(header["new_start"]),
                    new_lines=new_remaining,
                )
            )
            continue

        if old_remaining == 0 and new_remaining == 0:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            added += 1
            new_remaining -= 1
        elif line.startswith("-"):
            removed += 1
            old_remaining -= 1
        else:
            old_remaining -= 1
            new_remaining -= 1

    if not line_ranges:
        return None
    return ParsedUnifiedDiff(tuple(line_ranges), added, removed)
