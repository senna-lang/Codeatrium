"""Claude アダプターの extract_code_touches / edit_capability のテスト（design §5.2）"""

from codeatrium.adapters.harness.claude import edit_capability, extract_code_touches
from codeatrium.models import FileOnly, LineRange, TextAnchor


def _tool_use(tool_id: str, name: str, input_dict: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_dict}],
        },
    }


def _tool_result(tool_id: str, tool_use_result: dict, timestamp: str = "2026-08-08T00:00:00Z") -> dict:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": []}],
        },
        "toolUseResult": tool_use_result,
    }


def test_edit_capability_is_full() -> None:
    assert edit_capability() == "full"


def test_extract_edit_produces_line_range_and_text_anchor() -> None:
    entries = [
        _tool_use(
            "toolu_1",
            "Edit",
            {"file_path": "/repo/src/foo.py", "old_string": "a", "new_string": "b"},
        ),
        _tool_result(
            "toolu_1",
            {
                "filePath": "/repo/src/foo.py",
                "oldString": "a",
                "newString": "b",
                "structuredPatch": [
                    {"oldStart": 10, "oldLines": 1, "newStart": 10, "newLines": 1, "lines": ["-a", "+b"]}
                ],
                "originalFile": "...",
            },
        ),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    touch = touches[0]
    assert touch.harness == "claude"
    assert touch.tool_call_id == "toolu_1"
    assert touch.file_path == "/repo/src/foo.py"
    assert touch.touch_kind == "edit"
    assert touch.ts == "2026-08-08T00:00:00Z"
    assert touch.added == 1
    assert touch.removed == 1

    locator_types = [type(loc) for loc in touch.locators]
    assert LineRange in locator_types
    assert TextAnchor in locator_types
    assert FileOnly in locator_types

    line_range = next(loc for loc in touch.locators if isinstance(loc, LineRange))
    assert line_range.new_start == 10
    assert line_range.new_lines == 1

    anchor = next(loc for loc in touch.locators if isinstance(loc, TextAnchor))
    assert anchor.old_string == "a"
    assert anchor.new_string == "b"


def test_extract_write_produces_text_anchor_from_content() -> None:
    entries = [
        _tool_use("toolu_2", "Write", {"file_path": "/repo/src/new.py", "content": "print(1)\n"}),
        _tool_result(
            "toolu_2",
            {
                "filePath": "/repo/src/new.py",
                "content": "print(1)\n",
                "originalFile": None,
                "structuredPatch": [],
                "type": "create",
            },
        ),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    touch = touches[0]
    assert touch.touch_kind == "write"
    assert touch.added == 1
    assert touch.removed == 0

    anchor = next(loc for loc in touch.locators if isinstance(loc, TextAnchor))
    assert anchor.old_string is None
    assert anchor.new_string == "print(1)\n"
    assert any(isinstance(loc, FileOnly) for loc in touch.locators)


def test_extract_write_empty_new_file_still_recorded() -> None:
    """異常系: 新規ファイル（旧側が空）。中身も空でクラッシュしない"""
    entries = [
        _tool_use("toolu_3", "Write", {"file_path": "/repo/src/empty.py", "content": ""}),
        _tool_result(
            "toolu_3",
            {
                "filePath": "/repo/src/empty.py",
                "content": "",
                "originalFile": None,
                "structuredPatch": [],
                "type": "create",
            },
        ),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    assert touches[0].added == 0
    anchor = next(loc for loc in touches[0].locators if isinstance(loc, TextAnchor))
    assert anchor.new_string == ""


def test_extract_missing_structured_patch_and_strings_falls_back_to_file_only() -> None:
    """異常系: 差分の項目が無い"""
    entries = [
        _tool_use("toolu_4", "Edit", {"file_path": "/repo/src/foo.py", "old_string": "a", "new_string": "b"}),
        _tool_result("toolu_4", {"filePath": "/repo/src/foo.py"}),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    assert touches[0].locators == (FileOnly(),)


def test_extract_structured_patch_missing_line_numbers_is_skipped_not_crashed() -> None:
    """異常系: 行番号が欠けている patch エントリは読み飛ばす"""
    entries = [
        _tool_use("toolu_5", "Edit", {"file_path": "/repo/src/foo.py", "old_string": "a", "new_string": "b"}),
        _tool_result(
            "toolu_5",
            {
                "filePath": "/repo/src/foo.py",
                "oldString": "a",
                "newString": "b",
                "structuredPatch": [{"lines": ["-a", "+b"]}],
            },
        ),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    locator_types = [type(loc) for loc in touches[0].locators]
    assert LineRange not in locator_types
    assert TextAnchor in locator_types


def test_extract_relative_path_does_not_crash() -> None:
    """異常系: パスが相対（core の normalize_repo_path 側で弾く想定。adapter はそのまま通す）"""
    entries = [
        _tool_use("toolu_6", "Edit", {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"}),
        _tool_result("toolu_6", {"filePath": "src/foo.py", "oldString": "a", "newString": "b", "structuredPatch": []}),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    assert touches[0].file_path == "src/foo.py"


def test_extract_ignores_none_placeholder_entries() -> None:
    """異常系: 既インデックス領域の None プレースホルダを無視する"""
    entries = [None, None]
    touches = extract_code_touches(entries)
    assert touches == []


def test_extract_ignores_malformed_json_line_placeholder() -> None:
    """異常系: JSON として壊れた行はそもそも None プレースホルダか非 dict として渡ってくる想定"""
    entries = [None, {"unexpected": "shape"}, 42, "not-a-dict"]  # type: ignore[list-item]
    touches = extract_code_touches(entries)  # type: ignore[arg-type]
    assert touches == []


def test_extract_notebook_edit_falls_back_to_notebook_path_file_only() -> None:
    """NotebookEdit は独自形式が未検証のため FileOnly のみ（ファイル粒度は満たす）"""
    entries = [
        _tool_use(
            "toolu_9",
            "NotebookEdit",
            {"notebook_path": "/repo/analysis.ipynb", "new_source": "print(1)"},
        ),
        _tool_result("toolu_9", {}),
    ]

    touches = extract_code_touches(entries)

    assert len(touches) == 1
    touch = touches[0]
    assert touch.file_path == "/repo/analysis.ipynb"
    assert touch.touch_kind == "edit"
    assert touch.locators == (FileOnly(),)


def test_extract_ignores_read_tool_use() -> None:
    """Read は編集ではないため touch を作らない（今回のスコープ外）"""
    entries = [
        _tool_use("toolu_7", "Read", {"file_path": "/repo/src/foo.py"}),
        _tool_result("toolu_7", {"filePath": "/repo/src/foo.py", "content": "..."}),
    ]

    touches = extract_code_touches(entries)
    assert touches == []


def test_extract_falls_back_to_input_file_path_when_toolUseResult_missing_filePath() -> None:
    entries = [
        _tool_use("toolu_8", "Edit", {"file_path": "/repo/src/foo.py", "old_string": "a", "new_string": "b"}),
        _tool_result("toolu_8", {"oldString": "a", "newString": "b"}),
    ]

    touches = extract_code_touches(entries)
    assert len(touches) == 1
    assert touches[0].file_path == "/repo/src/foo.py"


def test_extract_tool_result_without_matching_tool_use_is_ignored() -> None:
    entries = [_tool_result("toolu_orphan", {"filePath": "/repo/src/foo.py"})]
    touches = extract_code_touches(entries)
    assert touches == []
