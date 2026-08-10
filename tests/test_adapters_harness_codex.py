"""Codex の patch_apply_end イベントから編集記録を抽出する契約を検証する。"""

import json
from pathlib import Path
from typing import Any

from codeatrium.adapters.harness.codex import edit_capability, extract_code_touches
from codeatrium.models import FileOnly, LineRange, TextAnchor

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "codex.jsonl"


def _entries() -> list[dict[str, Any] | None]:
    return [json.loads(line) for line in _FIXTURE.read_text().splitlines()]


def test_edit_capability_is_full() -> None:
    """Codex は unified diff から行範囲を直接記録できる。"""
    assert edit_capability() == "full"


def test_extract_code_touches_normalizes_update_add_and_delete() -> None:
    """同じ patch_apply_end 内の各ファイルを個別の編集記録へ正規化する。"""
    touches = extract_code_touches(_entries())

    assert len(touches) == 4

    update, addition, deletion, move_update = touches
    assert update.harness == "codex"
    assert update.tool_call_id == "call_synth001:/repo/src/fs.py"
    assert update.file_path == "/repo/src/fs.py"
    assert update.touch_kind == "edit"
    assert update.locators == (
        LineRange(old_start=1, old_lines=3, new_start=1, new_lines=6),
        FileOnly(),
    )
    assert (update.added, update.removed) == (5, 2)

    assert addition.file_path == "/repo/src/result.py"
    assert addition.touch_kind == "write"
    assert addition.locators == (
        TextAnchor(old_string=None, new_string="class Result:\n    pass\n"),
        FileOnly(),
    )
    assert (addition.added, addition.removed) == (2, 0)

    assert deletion.file_path == "/repo/src/legacy.py"
    assert deletion.touch_kind == "edit"
    assert deletion.locators == (
        TextAnchor(old_string="x = 1\n", new_string=None),
        FileOnly(),
    )
    assert (deletion.added, deletion.removed) == (0, 1)

    assert move_update.tool_call_id == "call_synth002:/repo/src/old_name.py"
    assert move_update.file_path == "/repo/src/old_name.py"
    assert move_update.locators == (
        LineRange(old_start=1, old_lines=1, new_start=1, new_lines=1),
        FileOnly(),
    )


def test_extract_code_touches_ignores_empty_changes() -> None:
    """変更対象を持たない正常イベントは編集記録にしない。"""
    assert extract_code_touches([_entries()[-1]]) == []
