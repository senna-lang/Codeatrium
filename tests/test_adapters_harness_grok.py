"""grok の ACP tool_call から編集記録を抽出する契約を検証する。

合成ログ（grok.jsonl）は実ログ39本の実測形を反映している:
同一 toolCallId への複数更新・相対 rawInput と絶対 diff パス・失敗呼び出し・
`_x.ai/session/update` 名前空間・hook_execution ノイズ。
"""

import json
from pathlib import Path
from typing import Any

from codeatrium.adapters.harness.grok import edit_capability, extract_code_touches
from codeatrium.models import FileOnly, TextAnchor

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "grok.jsonl"


def _entries() -> list[dict[str, Any] | None]:
    return [
        json.loads(line) for line in _FIXTURE.read_text().splitlines() if line.strip()
    ]


def _by_path(touches: list) -> dict[str, Any]:
    return {t.file_path: t for t in touches}


def test_edit_capability_is_anchor() -> None:
    """編集に行番号が無い（locations は read_file 等にしか付かない）ので anchor。"""
    assert edit_capability() == "anchor"


def test_one_touch_per_tool_call_despite_repeated_updates() -> None:
    """同じ toolCallId に diff が2回届く（実測595/600）。二重計上しない。"""
    touches = extract_code_touches(_entries())

    call_ids = [t.tool_call_id for t in touches]
    assert call_ids == sorted(set(call_ids), key=call_ids.index)
    assert len(touches) == 2


def test_search_replace_uses_old_and_new_string_as_anchor() -> None:
    """行番号が無いので、文字列アンカーだけを手がかりに残す。"""
    edit = _by_path(extract_code_touches(_entries()))["/repo/src/fs.py"]

    assert edit.harness == "grok"
    assert edit.touch_kind == "edit"
    anchor = edit.locators[0]
    assert isinstance(anchor, TextAnchor)
    assert anchor.old_string is not None
    assert "def list_dir(path):" in anchor.old_string
    assert "Result[list[str], OSError]" in (anchor.new_string or "")
    assert edit.locators[-1] == FileOnly()
    # LineRange は作らない（anchor capability の宣言と実装を一致させる）
    assert not [loc for loc in edit.locators if hasattr(loc, "new_start")]


def test_diff_path_wins_over_relative_raw_input_path() -> None:
    """rawInput.file_path は相対のことがある（実測60/600）。diff 側の絶対パスを使う。"""
    touches = _by_path(extract_code_touches(_entries()))

    assert "/repo/src/result.py" in touches
    assert "src/result.py" not in touches


def test_write_empty_old_text_becomes_none_anchor() -> None:
    """write の diff は `oldText: ""`。空文字は「一致なし」と紛らわしいので None に寄せる。"""
    write = _by_path(extract_code_touches(_entries()))["/repo/src/result.py"]

    assert write.touch_kind == "write"
    assert write.locators == (
        TextAnchor(old_string=None, new_string="class Result:\n    pass\n"),
        FileOnly(),
    )
    assert (write.added, write.removed) == (2, 0)


def test_integer_epoch_timestamp_is_normalized_to_iso() -> None:
    """grok の timestamp は整数 epoch 秒（実測819/819）。他ハーネスと同じ ISO に揃える。"""
    edit = _by_path(extract_code_touches(_entries()))["/repo/src/fs.py"]

    assert edit.ts is not None
    assert edit.ts.startswith("2026-")


def test_failed_tool_call_is_not_recorded() -> None:
    """old_string が見つからず失敗した編集は適用されていないので記録しない。"""
    touches = extract_code_touches(_entries())

    assert not [t for t in touches if "missing.py" in t.file_path]
    assert not [t for t in touches if t.tool_call_id == "call-synth-003"]


def test_non_edit_tools_are_ignored() -> None:
    """read_file など編集以外の tool_call は編集記録にしない。"""
    touches = extract_code_touches(_entries())

    assert not [t for t in touches if t.tool_call_id == "call-synth-004"]


def test_ignores_hook_and_other_session_update_kinds() -> None:
    """hook_execution / turn_completed などのノイズで落ちない（実測 hook は3651件）。"""
    entries = _entries()
    noise: list[dict[str, Any] | None] = [
        e
        for e in entries
        if isinstance(e, dict)
        and e["params"]["update"].get("sessionUpdate")
        not in ("tool_call", "tool_call_update")
    ]

    assert extract_code_touches(noise) == []


def test_ignores_none_placeholder_entries() -> None:
    """既インデックス領域の None プレースホルダで落ちない。"""
    assert extract_code_touches([None, None]) == []
