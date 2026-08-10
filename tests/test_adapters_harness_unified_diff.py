"""標準 unified diff を共有の行範囲・変更量へ正規化する契約を検証する。"""

from codeatrium.adapters.harness.unified_diff import parse_unified_diff
from codeatrium.models import LineRange


def test_parse_unified_diff_extracts_each_hunk_and_change_counts() -> None:
    """複数 hunk の新旧行範囲と実際の追加・削除行数を返す。"""
    diff = """@@ -1,3 +1,4 @@
 keep_one
-old_one
+new_one
+new_two
 keep_two
@@ -10 +11 @@ unchanged
-old_two
+new_three
"""

    parsed = parse_unified_diff(diff)

    assert parsed is not None
    assert parsed.line_ranges == (
        LineRange(old_start=1, old_lines=3, new_start=1, new_lines=4),
        LineRange(old_start=10, old_lines=1, new_start=11, new_lines=1),
    )
    assert parsed.added == 3
    assert parsed.removed == 2


def test_parse_unified_diff_rejects_text_without_hunks() -> None:
    """hunk を含まない文字列は行番号の根拠にしない。"""
    assert parse_unified_diff("Edit applied successfully.") is None


def test_parse_unified_diff_does_not_count_following_file_headers() -> None:
    """次のファイルのヘッダーは hunk の追加・削除行に数えない。"""
    diff = """@@ -1 +1 @@
-old_one
+new_one
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-old_two
+new_two
"""

    parsed = parse_unified_diff(diff)

    assert parsed is not None
    assert parsed.added == 2
    assert parsed.removed == 2
