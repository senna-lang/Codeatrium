"""Codex の patch_apply_end イベントから編集位置を CodeTouch へ正規化する。"""

from __future__ import annotations

from typing import Any

from codeatrium.adapters.harness.unified_diff import parse_unified_diff
from codeatrium.models import CodeLocator, CodeTouch, FileOnly, TextAnchor

_CHANGE_TYPES = frozenset({"add", "delete", "update"})


def edit_capability() -> str:
    """Codex は unified diff を持つため full を宣言する。"""
    return "full"


def extract_code_touches(raw_entries: list[dict[str, Any] | None]) -> list[CodeTouch]:
    """成功した patch_apply_end のファイル別変更を編集記録として返す。"""
    touches: list[CodeTouch] = []

    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "patch_apply_end":
            continue
        if payload.get("success") is not True:
            continue

        call_id = payload.get("call_id")
        changes = payload.get("changes")
        if not isinstance(call_id, str) or not call_id or not isinstance(changes, dict):
            continue

        timestamp = entry.get("timestamp")
        for file_path, change in changes.items():
            if not isinstance(file_path, str) or not isinstance(change, dict):
                continue
            change_type = change.get("type")
            if change_type not in _CHANGE_TYPES:
                continue

            parsed_diff = _parse_change_diff(change)
            locators = _build_locators(change_type, change, parsed_diff)
            added, removed = _change_count(change_type, change, parsed_diff)
            touches.append(
                CodeTouch(
                    harness="codex",
                    tool_call_id=f"{call_id}:{file_path}",
                    file_path=file_path,
                    touch_kind="write" if change_type == "add" else "edit",
                    locators=locators,
                    added=added,
                    removed=removed,
                    ts=timestamp if isinstance(timestamp, str) else None,
                )
            )

    return touches


def extract_file_renames(
    raw_entries: list[dict[str, Any] | None],
) -> list[tuple[str, str, str | None]]:
    """成功した patch_apply_end の move_path を旧新パスと時刻で返す。"""
    renames: list[tuple[str, str, str | None]] = []

    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "patch_apply_end":
            continue
        if payload.get("success") is not True:
            continue

        changes = payload.get("changes")
        if not isinstance(changes, dict):
            continue
        timestamp = entry.get("timestamp")
        ts = timestamp if isinstance(timestamp, str) else None
        for old_path, change in changes.items():
            if not isinstance(old_path, str) or not isinstance(change, dict):
                continue
            new_path = change.get("move_path")
            if isinstance(new_path, str) and new_path and new_path != old_path:
                renames.append((old_path, new_path, ts))

    return renames


def _parse_change_diff(change: dict[str, Any]):
    diff = change.get("unified_diff")
    return parse_unified_diff(diff) if isinstance(diff, str) else None


def _build_locators(
    change_type: str,
    change: dict[str, Any],
    parsed_diff: Any,
) -> tuple[CodeLocator, ...]:
    locators: list[CodeLocator] = []
    if parsed_diff is not None:
        locators.extend(parsed_diff.line_ranges)

    content = change.get("content")
    if isinstance(content, str):
        if change_type == "add":
            locators.append(TextAnchor(old_string=None, new_string=content))
        elif change_type == "delete":
            locators.append(TextAnchor(old_string=content, new_string=None))

    locators.append(FileOnly())
    return tuple(locators)


def _change_count(
    change_type: str,
    change: dict[str, Any],
    parsed_diff: Any,
) -> tuple[int, int]:
    if parsed_diff is not None:
        return parsed_diff.added, parsed_diff.removed

    content = change.get("content")
    if not isinstance(content, str) or not content:
        return 0, 0
    line_count = len(content.splitlines())
    if change_type == "add":
        return line_count, 0
    if change_type == "delete":
        return 0, line_count
    return 0, 0
