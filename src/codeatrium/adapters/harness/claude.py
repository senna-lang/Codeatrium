"""
Claude Code のセッションログから編集記録（CodeTouch）を取り出すアダプター。

`toolUseResult.structuredPatch` を LineRange へ、`oldString`/`newString` を
TextAnchor へ正規化して core へ渡す（design §3.4 の port）。
`structuredPatch` は assistant の tool_use エントリではなく、対応する
user エントリの `toolUseResult` に載っている点に注意（tool_use_id で対応付ける）。
行の割り出しやシンボル特定はここでは行わない — それは core の仕事。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codeatrium.models import (
    CodeLocator,
    CodeTouch,
    EditCapability,
    FileOnly,
    LineRange,
    TextAnchor,
)

_WRITE_TOOL_NAME = "Write"
_NOTEBOOK_TOOL_NAME = "NotebookEdit"
_TRACKED_TOOL_NAMES = {"Edit", "MultiEdit", _WRITE_TOOL_NAME, _NOTEBOOK_TOOL_NAME}

_SUBAGENT_PATH_RE = re.compile(
    r"^(?P<parent_dir>.*)/"
    r"(?P<parent_uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"/subagents/.*\.jsonl$"
)


def parent_session_ref(path: Path) -> str | None:
    """サブエージェントの transcript パスから親セッションの参照を返す（design §2.3・§4.2）。

    Claude Code の規約: `<project>/<親セッションUUID>/subagents/...` の下（任意の深さの
    サブディレクトリを許す。実測で `subagents/workflows/wf_X/agent-Y.jsonl` のような
    ネストも観測されたため、`subagents/` 直下1階層に限定しない）に
    サブエージェントのログが置かれ、親は `<project>/<親セッションUUID>.jsonl`。
    実コーパス（本リポジトリの .codeatrium/memory.db、該当477件）で
    100%一致・親セッションが実在することを検証済み。

    一致しなければ None（親セッション自身、または規約外のパス）。
    """
    m = _SUBAGENT_PATH_RE.match(str(path))
    if m is None:
        return None
    return f"{m.group('parent_dir')}/{m.group('parent_uuid')}.jsonl"


def edit_capability() -> EditCapability:
    """Claude Code は structuredPatch で行番号まで出せる（design §3.3）"""
    return "full"


def extract_code_touches(raw_entries: list[dict[str, Any] | None]) -> list[CodeTouch]:
    """ログのエントリ列から編集記録を取り出す。壊れた・想定外の形のエントリは無視して続行する。"""
    tool_uses = _collect_tracked_tool_uses(raw_entries)

    touches: list[CodeTouch] = []
    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        tur = entry.get("toolUseResult")
        if not isinstance(tur, dict):
            continue

        for tool_id in _tool_result_ids(entry):
            tracked = tool_uses.get(tool_id)
            if tracked is None:
                continue
            name, input_dict = tracked

            file_path = tur.get("filePath")
            if not isinstance(file_path, str) or not file_path:
                key = "notebook_path" if name == _NOTEBOOK_TOOL_NAME else "file_path"
                file_path = input_dict.get(key)
            if not isinstance(file_path, str) or not file_path:
                continue

            timestamp = entry.get("timestamp")
            added, removed = _count_added_removed(name, tur)

            touches.append(
                CodeTouch(
                    harness="claude",
                    tool_call_id=tool_id,
                    file_path=file_path,
                    touch_kind="write" if name == _WRITE_TOOL_NAME else "edit",
                    locators=_build_locators(name, tur),
                    added=added,
                    removed=removed,
                    ts=timestamp if isinstance(timestamp, str) else None,
                )
            )

    return touches


def _collect_tracked_tool_uses(
    raw_entries: list[dict[str, Any] | None],
) -> dict[str, tuple[str, dict[str, Any]]]:
    """assistant エントリの tool_use ブロックから id -> (name, input) を集める"""
    tool_uses: dict[str, tuple[str, dict[str, Any]]] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name not in _TRACKED_TOOL_NAMES:
                continue
            tool_id = block.get("id")
            input_dict = block.get("input")
            if not isinstance(tool_id, str) or not isinstance(input_dict, dict):
                continue
            tool_uses[tool_id] = (name, input_dict)
    return tool_uses


def _tool_result_ids(user_entry: dict[str, Any]) -> list[str]:
    msg = user_entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    ids: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_id = block.get("tool_use_id")
        if isinstance(tool_id, str):
            ids.append(tool_id)
    return ids


def _iter_valid_patches(structured_patch: Any) -> list[dict[str, Any]]:
    """壊れた・行番号が欠けた patch エントリは黙って読み飛ばす"""
    if not isinstance(structured_patch, list):
        return []
    valid: list[dict[str, Any]] = []
    for patch in structured_patch:
        if not isinstance(patch, dict):
            continue
        if not isinstance(patch.get("newStart"), int) or not isinstance(patch.get("newLines"), int):
            continue
        valid.append(patch)
    return valid


def _build_locators(tool_name: str, tur: dict[str, Any]) -> tuple[CodeLocator, ...]:
    """優先順位の高い順に手がかりを積む。何も無くても FileOnly は必ず含める（不変条件1の土台）"""
    if tool_name == _NOTEBOOK_TOOL_NAME:
        # NotebookEdit の独自形式は未検証。解読できない形式を full と申告しない（design §3.3）
        return (FileOnly(),)

    locators: list[CodeLocator] = []

    for patch in _iter_valid_patches(tur.get("structuredPatch")):
        old_start = patch.get("oldStart")
        old_lines = patch.get("oldLines")
        locators.append(
            LineRange(
                old_start=old_start if isinstance(old_start, int) else None,
                old_lines=old_lines if isinstance(old_lines, int) else None,
                new_start=patch["newStart"],
                new_lines=patch["newLines"],
            )
        )

    if tool_name == _WRITE_TOOL_NAME:
        content = tur.get("content")
        if isinstance(content, str):
            locators.append(TextAnchor(old_string=None, new_string=content))
    else:
        old_string = tur.get("oldString")
        new_string = tur.get("newString")
        if isinstance(old_string, str) or isinstance(new_string, str):
            locators.append(
                TextAnchor(
                    old_string=old_string if isinstance(old_string, str) else None,
                    new_string=new_string if isinstance(new_string, str) else None,
                )
            )

    locators.append(FileOnly())
    return tuple(locators)


def _count_added_removed(tool_name: str, tur: dict[str, Any]) -> tuple[int, int]:
    if tool_name == _WRITE_TOOL_NAME:
        content = tur.get("content")
        if isinstance(content, str) and content:
            return (len(content.splitlines()), 0)
        return (0, 0)

    added = 0
    removed = 0
    for patch in _iter_valid_patches(tur.get("structuredPatch")):
        lines = patch.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, str):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return (added, removed)
