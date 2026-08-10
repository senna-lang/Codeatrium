"""OpenCode の保存済み part から編集位置を CodeTouch へ正規化する。"""

from __future__ import annotations

from typing import Any

from codeatrium.adapters.harness.unified_diff import parse_unified_diff
from codeatrium.models import CodeLocator, CodeTouch, FileOnly, TextAnchor

_EDIT_TOOL_NAMES = frozenset({"edit", "write"})


def edit_capability() -> str:
    """OpenCode は unified diff を持つため full を宣言する。"""
    return "full"


def extract_code_touches(raw_entries: list[dict[str, Any] | None]) -> list[CodeTouch]:
    """完了した edit/write part を、行範囲・文字列アンカー付きで返す。"""
    touches: list[CodeTouch] = []

    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or data.get("type") != "tool":
            continue

        tool_name = data.get("tool")
        state = data.get("state")
        if tool_name not in _EDIT_TOOL_NAMES or not isinstance(state, dict):
            continue
        if state.get("status") != "completed":
            continue

        input_data = state.get("input")
        metadata = state.get("metadata")
        if not isinstance(input_data, dict):
            continue
        if not isinstance(metadata, dict):
            metadata = {}

        file_path = _file_path(metadata, input_data)
        tool_call_id = _tool_call_id(data, entry)
        if file_path is None or tool_call_id is None:
            continue

        parsed_diff = _parse_metadata_diff(metadata)
        locators = _build_locators(tool_name, input_data, parsed_diff)
        added, removed = _change_count(tool_name, input_data, parsed_diff)
        timestamp = entry.get("timestamp")

        touches.append(
            CodeTouch(
                harness="opencode",
                tool_call_id=tool_call_id,
                file_path=file_path,
                touch_kind="write" if tool_name == "write" else "edit",
                locators=locators,
                added=added,
                removed=removed,
                ts=timestamp if isinstance(timestamp, str) else None,
            )
        )

    return touches


def _file_path(metadata: dict[str, Any], input_data: dict[str, Any]) -> str | None:
    """絶対パスの metadata.filepath を相対指定の input より優先する。"""
    metadata_path = metadata.get("filepath")
    if isinstance(metadata_path, str) and metadata_path:
        return metadata_path

    input_path = input_data.get("filePath")
    return input_path if isinstance(input_path, str) and input_path else None


def _tool_call_id(data: dict[str, Any], entry: dict[str, Any]) -> str | None:
    call_id = data.get("callID")
    if isinstance(call_id, str) and call_id:
        return call_id

    part_id = entry.get("id")
    return part_id if isinstance(part_id, str) and part_id else None


def _parse_metadata_diff(metadata: dict[str, Any]):
    diff = metadata.get("diff")
    return parse_unified_diff(diff) if isinstance(diff, str) else None


def _build_locators(
    tool_name: str,
    input_data: dict[str, Any],
    parsed_diff: Any,
) -> tuple[CodeLocator, ...]:
    locators: list[CodeLocator] = []
    if parsed_diff is not None:
        locators.extend(parsed_diff.line_ranges)

    if tool_name == "write":
        content = input_data.get("content")
        if isinstance(content, str):
            locators.append(TextAnchor(old_string=None, new_string=content))
    else:
        old_string = input_data.get("oldString")
        new_string = input_data.get("newString")
        if isinstance(old_string, str) or isinstance(new_string, str):
            locators.append(
                TextAnchor(
                    old_string=old_string if isinstance(old_string, str) else None,
                    new_string=new_string if isinstance(new_string, str) else None,
                )
            )

    locators.append(FileOnly())
    return tuple(locators)


def _change_count(
    tool_name: str,
    input_data: dict[str, Any],
    parsed_diff: Any,
) -> tuple[int, int]:
    if parsed_diff is not None:
        return parsed_diff.added, parsed_diff.removed

    if tool_name == "write":
        content = input_data.get("content")
        if isinstance(content, str) and content:
            return len(content.splitlines()), 0
    return 0, 0
