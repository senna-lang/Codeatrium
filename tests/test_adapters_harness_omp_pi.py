"""omp-pi の toolCall から編集記録を抽出する契約を検証する。

合成ログ（omp_pi.jsonl）は実ログ99本の実測形を反映している:
相対パス・`*** Begin Patch` 前置き・複数ファイルパッチ・`edits` 配列・ヘッダ無し異常系。
"""

import json
from pathlib import Path
from typing import Any

from codeatrium.adapters.harness.omp_pi import (
    edit_capability,
    extract_code_touches,
    extract_file_renames,
)
from codeatrium.models import FileOnly, TextAnchor

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "omp_pi.jsonl"


def _entries(cwd: str | None = "/repo") -> list[dict[str, Any] | None]:
    """フィクスチャを読み、indexer が行うのと同じ cwd スタンプを付ける。"""
    entries: list[dict[str, Any] | None] = [
        json.loads(line) for line in _FIXTURE.read_text().splitlines() if line.strip()
    ]
    if cwd is not None:
        for entry in entries:
            if isinstance(entry, dict):
                entry["cwd"] = cwd
    return entries


def _by_path(touches: list) -> dict[str, Any]:
    return {t.file_path: t for t in touches}


def test_edit_capability_is_anchor() -> None:
    """独自 DSL を解読しないので full ではなく anchor を宣言する（design §3.3）。"""
    assert edit_capability() == "anchor"


def test_tool_result_path_wins_over_truncated_call_header() -> None:
    """toolCall のヘッダは入れ子ディレクトリ基準に切り詰められることがある（実測95件）。

    ヘッダ（fs.py）をそのまま cwd と結合すると存在しない /repo/fs.py になる。
    toolResult 側の解決済みパス（src/fs.py）を正として使う。
    """
    touches = _by_path(extract_code_touches(_entries()))

    assert "/repo/src/fs.py" in touches
    assert "/repo/fs.py" not in touches


def test_relative_header_path_is_absolutized_with_session_cwd() -> None:
    """実ログの大半は相対パス。cwd で絶対化しないと不変条件3で全部落ちる。"""
    touches = _by_path(extract_code_touches(_entries()))

    edit = touches["/repo/src/fs.py"]
    assert edit.harness == "omp-pi"
    assert edit.touch_kind == "edit"
    assert edit.tool_call_id == "call-synth-001:src/fs.py"
    # DSL は解読せず、本文をそのまま new_string に載せる
    assert isinstance(edit.locators[0], TextAnchor)
    assert edit.locators[0].old_string is None
    assert "def list_dir(path) -> Result[list[str], OSError]:" in (
        edit.locators[0].new_string or ""
    )
    assert edit.locators[-1] == FileOnly()
    assert (edit.added, edit.removed) == (5, 0)


def test_write_uses_path_argument_and_content_anchor() -> None:
    """write は input ではなく path/content を持つ。partialArgs 付きでも完了扱い。"""
    touches = _by_path(extract_code_touches(_entries()))

    write = touches["/repo/src/result.py"]
    assert write.touch_kind == "write"
    assert write.tool_call_id == "call-synth-002"
    assert write.locators == (
        TextAnchor(old_string=None, new_string="class Result:\n    pass\n"),
        FileOnly(),
    )
    assert (write.added, write.removed) == (2, 0)


def test_multi_file_patch_produces_one_touch_per_file() -> None:
    """1回の toolCall が複数ファイルを含む（実測で最大6件）。全ファイルを記録する。"""
    touches = _by_path(extract_code_touches(_entries()))

    assert "/repo/src/types.ts" in touches
    assert "/repo/src/core/util.ts" in touches

    types_touch = touches["/repo/src/types.ts"]
    util_touch = touches["/repo/src/core/util.ts"]
    assert types_touch.tool_call_id == "call-synth-003:src/types.ts"
    assert util_touch.tool_call_id == "call-synth-003:src/core/util.ts"

    # 各ファイルのアンカーは自分の区間だけを持つ（丸ごと複製しない）
    types_anchor = types_touch.locators[0]
    util_anchor = util_touch.locators[0]
    assert isinstance(types_anchor, TextAnchor)
    assert isinstance(util_anchor, TextAnchor)
    assert "streamMessage(" in (types_anchor.new_string or "")
    assert "streamMessage(" not in (util_anchor.new_string or "")
    assert "export const noop" in (util_anchor.new_string or "")
    assert "export const noop" not in (types_anchor.new_string or "")


def test_begin_patch_preamble_does_not_hide_the_header() -> None:
    """`*** Begin Patch` は前置きで、ヘッダは常にその次行にある（実ログ 263/263）。"""
    touches = _by_path(extract_code_touches(_entries()))

    # 前置きを読み飛ばせていなければ types.ts は取れない
    assert "/repo/src/types.ts" in touches


def test_edits_array_maps_directly_to_text_anchor() -> None:
    """`edits` 配列（old_text/new_text）は DSL を経由せず TextAnchor へ写せる。"""
    touches = _by_path(extract_code_touches(_entries()))

    limits = touches["/repo/src/limits.py"]
    assert limits.tool_call_id == "call-synth-004:0"
    assert limits.locators == (
        TextAnchor(
            old_string="if remaining != 30:",
            new_string="if remaining != DEFAULT_BUCKET:",
        ),
        FileOnly(),
    )


def test_uri_scheme_write_target_is_not_recorded_as_a_file() -> None:
    """`xd://...`（MCP ツール呼び出し先、実測326件）はファイルではないので記録しない。

    cwd と結合すると <cwd>/xd:/... というリポジトリ内の実在しないパスに化ける。
    """
    touches = extract_code_touches(_entries())

    assert not [t for t in touches if "xd:" in t.file_path]
    assert not [t for t in touches if t.tool_call_id.startswith("call-synth-007")]


def test_patch_without_header_or_path_is_not_recorded() -> None:
    """場所が特定できないパッチ（実測12件）は推測せず記録しない（design §3.3）。"""
    touches = extract_code_touches(_entries())

    # call-synth-006 はヘッダも path も無い
    assert not [t for t in touches if t.tool_call_id.startswith("call-synth-006")]


def test_relative_path_without_cwd_stays_relative_and_is_dropped_later() -> None:
    """cwd が取れないときは勝手に補わない。相対のまま返し、不変条件3で落とす。"""
    touches = _by_path(extract_code_touches(_entries(cwd=None)))

    assert "src/result.py" in touches
    assert "/repo/src/result.py" not in touches


def test_extract_file_renames_reads_mv_command() -> None:
    """`MV 旧 -> 新` は行番号を含まないので安全に読める（design §8.2 段1）。"""
    renames = extract_file_renames(_entries())

    assert renames == [
        ("/repo/src/legacy.py", "/repo/src/core/legacy.py", "2026-08-01T00:00:10.000Z")
    ]


def test_extract_ignores_non_assistant_and_non_message_entries() -> None:
    """user/developer/toolResult や custom envelope から編集記録を作らない。"""
    entries = _entries()
    non_assistant: list[dict[str, Any] | None] = [
        e
        for e in entries
        if isinstance(e, dict)
        and (
            e.get("type") != "message"
            or (e.get("message") or {}).get("role") != "assistant"
        )
    ]

    assert extract_code_touches(non_assistant) == []


def test_extract_ignores_none_placeholder_entries() -> None:
    """既インデックス領域の None プレースホルダで落ちない。"""
    assert extract_code_touches([None, None]) == []
