"""normalize_repo_path / build_code_touch_rows のテスト（design §5.3・§8.1 不変条件3）"""

from codeatrium.code_touches import (
    build_code_touch_rows,
    is_external_path,
    normalize_repo_path,
)
from codeatrium.models import CodeTouch, FileOnly, LineRange, TextAnchor


def test_normalize_repo_path_inside_project_returns_relative() -> None:
    result = normalize_repo_path(
        "/Users/x/repo/src/codeatrium/db.py", "/Users/x/repo"
    )
    assert result == "src/codeatrium/db.py"


def test_normalize_repo_path_outside_project_returns_none() -> None:
    result = normalize_repo_path("/tmp/scratch/foo.py", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_neighboring_repo_with_shared_prefix_returns_none() -> None:
    """不具合G: 文字列の前方一致だけで判定すると隣のリポジトリを誤って内部と判定してしまう"""
    result = normalize_repo_path(
        "/Users/x/repo-other/foo.py", "/Users/x/repo"
    )
    assert result is None


def test_normalize_repo_path_project_root_itself_returns_none() -> None:
    result = normalize_repo_path("/Users/x/repo", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_relative_input_returns_none() -> None:
    result = normalize_repo_path("src/db.py", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_trailing_slash_on_root_is_tolerated() -> None:
    result = normalize_repo_path(
        "/Users/x/repo/src/db.py", "/Users/x/repo/"
    )
    assert result == "src/db.py"


def test_normalize_repo_path_dotdot_escaping_root_returns_none() -> None:
    result = normalize_repo_path(
        "/Users/x/repo/../secrets/foo.py", "/Users/x/repo"
    )
    assert result is None


def test_normalize_repo_path_excludes_venv() -> None:
    result = normalize_repo_path(
        "/Users/x/repo/.venv/lib/python3.11/site-packages/foo/bar.py", "/Users/x/repo"
    )
    assert result is None


def test_normalize_repo_path_excludes_node_modules() -> None:
    result = normalize_repo_path(
        "/Users/x/repo/node_modules/react/index.js", "/Users/x/repo"
    )
    assert result is None


def test_is_external_path_site_packages() -> None:
    assert is_external_path("some/dir/site-packages/pkg/mod.py") is True


def test_is_external_path_project_file() -> None:
    assert is_external_path("src/codeatrium/db.py") is False


def test_build_code_touch_rows_line_and_anchor_together_produce_one_row() -> None:
    touch = CodeTouch(
        harness="claude",
        tool_call_id="toolu_1",
        file_path="/repo/src/foo.py",
        touch_kind="edit",
        locators=(
            LineRange(old_start=10, old_lines=1, new_start=10, new_lines=1),
            TextAnchor(old_string="a", new_string="b"),
            FileOnly(),
        ),
        added=1,
        removed=1,
        ts="2026-08-08T00:00:00Z",
    )

    rows = build_code_touch_rows(touch, exchange_id="ex1", rel_file_path="src/foo.py")

    assert len(rows) == 1
    row = rows[0]
    assert row["exchange_id"] == "ex1"
    assert row["harness"] == "claude"
    assert row["tool_call_id"] == "toolu_1"
    assert row["file_path"] == "src/foo.py"
    assert row["touch_kind"] == "edit"
    assert row["locator_kind"] == "line"
    assert row["new_start"] == 10
    assert row["new_lines"] == 1
    # 生データはそのまま保存する（principle②）: anchor も同じ行に残す
    assert row["old_string"] == "a"
    assert row["new_string"] == "b"
    assert row["added"] == 1
    assert row["removed"] == 1
    assert row["ts"] == "2026-08-08T00:00:00Z"


def test_build_code_touch_rows_multiple_line_ranges_produce_one_row_per_hunk() -> None:
    touch = CodeTouch(
        harness="claude",
        tool_call_id="toolu_2",
        file_path="/repo/src/foo.py",
        touch_kind="edit",
        locators=(
            LineRange(old_start=10, old_lines=1, new_start=10, new_lines=1),
            LineRange(old_start=50, old_lines=2, new_start=51, new_lines=3),
            FileOnly(),
        ),
        added=4,
        removed=3,
        ts=None,
    )

    rows = build_code_touch_rows(touch, exchange_id="ex1", rel_file_path="src/foo.py")

    assert len(rows) == 2
    assert {row["new_start"] for row in rows} == {10, 51}
    assert len({row["id"] for row in rows}) == 2  # id は seq を含むので衝突しない


def test_build_code_touch_rows_anchor_only() -> None:
    touch = CodeTouch(
        harness="grok",
        tool_call_id="call_1",
        file_path="/repo/src/foo.py",
        touch_kind="edit",
        locators=(TextAnchor(old_string="a", new_string="b"), FileOnly()),
        added=0,
        removed=0,
        ts=None,
    )

    rows = build_code_touch_rows(touch, exchange_id="ex1", rel_file_path="src/foo.py")

    assert len(rows) == 1
    assert rows[0]["locator_kind"] == "anchor"
    assert rows[0]["new_start"] is None


def test_build_code_touch_rows_empty_locators_still_produces_one_file_row() -> None:
    """不変条件1の土台: 手がかりが1つも無くても最低1行は作る"""
    touch = CodeTouch(
        harness="future-harness",
        tool_call_id="call_1",
        file_path="/repo/src/foo.py",
        touch_kind="edit",
        locators=(),
        added=0,
        removed=0,
        ts=None,
    )

    rows = build_code_touch_rows(touch, exchange_id="ex1", rel_file_path="src/foo.py")

    assert len(rows) == 1
    assert rows[0]["locator_kind"] == "file"


def test_build_code_touch_rows_file_only() -> None:
    touch = CodeTouch(
        harness="claude",
        tool_call_id="toolu_3",
        file_path="/repo/src/foo.py",
        touch_kind="edit",
        locators=(FileOnly(),),
        added=0,
        removed=0,
        ts=None,
    )

    rows = build_code_touch_rows(touch, exchange_id="ex1", rel_file_path="src/foo.py")

    assert len(rows) == 1
    row = rows[0]
    assert row["locator_kind"] == "file"
    assert row["new_start"] is None
    assert row["old_string"] is None
