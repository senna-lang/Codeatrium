"""OpenCode の保存済み part から編集記録を抽出する契約を検証する。"""

import json
from pathlib import Path
from typing import Any

from codeatrium.adapters.harness.opencode import edit_capability, extract_code_touches
from codeatrium.models import FileOnly, LineRange, TextAnchor

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "opencode.json"


def _parts() -> list[dict[str, Any] | None]:
    return json.loads(_FIXTURE.read_text())["parts"]


def test_edit_capability_is_full() -> None:
    """OpenCode は unified diff から行範囲を直接記録できる。"""
    assert edit_capability() == "full"


def test_extract_code_touches_normalizes_completed_edits_and_writes() -> None:
    """絶対パスを優先し、diff と文字列アンカーを併記する。"""
    touches = extract_code_touches(_parts())

    assert len(touches) == 2

    edit, write = touches
    assert edit.harness == "opencode"
    assert edit.tool_call_id == "call_synth001"
    assert edit.file_path == "/repo/src/fs.py"
    assert edit.touch_kind == "edit"
    assert edit.locators == (
        LineRange(old_start=1, old_lines=3, new_start=1, new_lines=6),
        TextAnchor(
            old_string="def list_dir(path):\n    return os.listdir(path)\n",
            new_string="def list_dir(path) -> Result[list[str], OSError]:\n    try:\n        return Ok(os.listdir(path))\n    except OSError as e:\n        return Err(e)\n",
        ),
        FileOnly(),
    )
    assert (edit.added, edit.removed) == (5, 2)

    assert write.harness == "opencode"
    assert write.tool_call_id == "call_synth002"
    assert write.file_path == "/repo/src/result.py"
    assert write.touch_kind == "write"
    assert write.locators == (
        TextAnchor(old_string=None, new_string="class Result:\n    pass\n"),
        FileOnly(),
    )
    assert (write.added, write.removed) == (2, 0)


def test_extract_code_touches_ignores_failed_parts() -> None:
    """権限拒否など完了していないツール呼び出しは編集記録にしない。"""
    failed_part = next(p for p in _parts() if p is not None and p["id"] == "prt_synth5")

    assert extract_code_touches([failed_part]) == []
