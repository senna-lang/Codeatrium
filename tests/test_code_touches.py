"""normalize_repo_path / build_code_touch_rows のテスト（design §5.3・§8.1 不変条件3）"""

from pathlib import Path

from codeatrium.code_touches import (
    build_code_touch_rows,
    intersect_span,
    is_external_path,
    normalize_repo_path,
    touches_to_edges,
)
from codeatrium.models import CodeTouch, FileOnly, LineRange, TextAnchor
from codeatrium.resolver import Symbol
from codeatrium.utils import sha256


def test_normalize_repo_path_inside_project_returns_relative() -> None:
    result = normalize_repo_path("/Users/x/repo/src/codeatrium/db.py", "/Users/x/repo")
    assert result == "src/codeatrium/db.py"


def test_normalize_repo_path_outside_project_returns_none() -> None:
    result = normalize_repo_path("/tmp/scratch/foo.py", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_neighboring_repo_with_shared_prefix_returns_none() -> None:
    """不具合G: 文字列の前方一致だけで判定すると隣のリポジトリを誤って内部と判定してしまう"""
    result = normalize_repo_path("/Users/x/repo-other/foo.py", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_project_root_itself_returns_none() -> None:
    result = normalize_repo_path("/Users/x/repo", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_relative_input_returns_none() -> None:
    result = normalize_repo_path("src/db.py", "/Users/x/repo")
    assert result is None


def test_normalize_repo_path_trailing_slash_on_root_is_tolerated() -> None:
    result = normalize_repo_path("/Users/x/repo/src/db.py", "/Users/x/repo/")
    assert result == "src/db.py"


def test_normalize_repo_path_resolves_equivalent_symlink_paths(tmp_path: Path) -> None:
    """ログの symlink 経由パスも、実体側 project root の配下として扱う。"""
    project_root = tmp_path / "project"
    source_file = project_root / "src" / "module.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("value = 1\n")
    linked_root = tmp_path / "project-link"
    linked_root.symlink_to(project_root, target_is_directory=True)

    result = normalize_repo_path(
        str(linked_root / "src" / "module.py"),
        str(project_root),
    )

    assert result == "src/module.py"


def test_normalize_repo_path_dotdot_escaping_root_returns_none() -> None:
    result = normalize_repo_path("/Users/x/repo/../secrets/foo.py", "/Users/x/repo")
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


# ---- intersect_span / touches_to_edges（design §5.5・§8.1 不変条件1・2） ----


def _symbol(name: str, line: int, end_line: int) -> Symbol:
    return Symbol(
        symbol_name=name,
        symbol_kind="function",
        signature=f"def {name}():",
        line=line,
        end_line=end_line,
        file_path="src/foo.py",
        lang=".py",
    )


def _touch(*line_ranges: LineRange, touch_kind: str = "edit") -> CodeTouch:
    return CodeTouch(
        harness="claude",
        tool_call_id="toolu_1",
        file_path="/repo/src/foo.py",
        touch_kind=touch_kind,  # type: ignore[arg-type]
        locators=(*line_ranges, FileOnly()),
        added=1,
        removed=0,
        ts="2026-08-09T00:00:00Z",
    )


def test_intersect_span_touch_inside_symbol_matches() -> None:
    touch = _touch(LineRange(old_start=6, old_lines=1, new_start=6, new_lines=1))
    symbols = [_symbol("greet", line=5, end_line=8)]

    matched = intersect_span(touch, symbols)

    assert [s.symbol_name for s in matched] == ["greet"]


def test_intersect_span_touch_between_symbols_returns_empty() -> None:
    """モジュールレベルの兄弟コードを誤って包含しない（§2.4「自信満々で間違える」対策）"""
    touch = _touch(LineRange(old_start=10, old_lines=1, new_start=10, new_lines=1))
    symbols = [_symbol("a", line=1, end_line=5), _symbol("b", line=20, end_line=25)]

    matched = intersect_span(touch, symbols)

    assert matched == []


def test_intersect_span_boundary_overlap_counts_as_match() -> None:
    """touch の終端行 == シンボルの開始行 のような端の重なりも一致とみなす"""
    touch = _touch(LineRange(old_start=5, old_lines=1, new_start=5, new_lines=1))
    symbols = [_symbol("greet", line=5, end_line=8)]

    matched = intersect_span(touch, symbols)

    assert [s.symbol_name for s in matched] == ["greet"]


def test_intersect_span_multiple_hunks_match_distinct_symbols() -> None:
    touch = _touch(
        LineRange(old_start=2, old_lines=1, new_start=2, new_lines=1),
        LineRange(old_start=20, old_lines=1, new_start=20, new_lines=1),
    )
    symbols = [_symbol("a", line=1, end_line=5), _symbol("b", line=18, end_line=22)]

    matched = intersect_span(touch, symbols)

    assert {s.symbol_name for s in matched} == {"a", "b"}


def test_intersect_span_multiple_hunks_same_symbol_deduped() -> None:
    touch = _touch(
        LineRange(old_start=2, old_lines=1, new_start=2, new_lines=1),
        LineRange(old_start=4, old_lines=1, new_start=4, new_lines=1),
    )
    symbols = [_symbol("a", line=1, end_line=5)]

    matched = intersect_span(touch, symbols)

    assert len(matched) == 1


def test_intersect_span_no_line_range_locator_returns_empty() -> None:
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

    matched = intersect_span(touch, [_symbol("a", line=1, end_line=5)])

    assert matched == []


def test_intersect_span_empty_symbols_returns_empty() -> None:
    touch = _touch(LineRange(old_start=2, old_lines=1, new_start=2, new_lines=1))

    assert intersect_span(touch, []) == []


def test_touches_to_edges_matched_symbol_produces_line_edge() -> None:
    touch = _touch(LineRange(old_start=6, old_lines=1, new_start=6, new_lines=1))
    symbols = [_symbol("greet", line=5, end_line=8)]

    edges = touches_to_edges(touch, "ex1", "src/foo.py", symbols)

    assert len(edges) == 1
    edge = edges[0]
    assert edge.granularity == "line"
    assert edge.confidence == 1.0
    assert edge.symbol_id == sha256("src/foo.py:greet")
    assert edge.exchange_id == "ex1"
    assert edge.file_path == "src/foo.py"
    assert edge.edge_kind == "edit"
    assert edge.added == 1
    assert edge.ts == "2026-08-09T00:00:00Z"


def test_touches_to_edges_no_symbols_falls_back_to_file_granularity() -> None:
    """不変条件2: シンボル未解決（未対応言語など）でもファイル粒度で必ず1本張る"""
    touch = _touch(LineRange(old_start=6, old_lines=1, new_start=6, new_lines=1))

    edges = touches_to_edges(touch, "ex1", "src/foo.md", [])

    assert len(edges) == 1
    edge = edges[0]
    assert edge.granularity == "file"
    assert edge.symbol_id is None
    assert edge.confidence == 0.5


def test_touches_to_edges_touch_between_symbols_falls_back_to_file_granularity() -> (
    None
):
    touch = _touch(LineRange(old_start=10, old_lines=1, new_start=10, new_lines=1))
    symbols = [_symbol("a", line=1, end_line=5), _symbol("b", line=20, end_line=25)]

    edges = touches_to_edges(touch, "ex1", "src/foo.py", symbols)

    assert len(edges) == 1
    assert edges[0].granularity == "file"
    assert edges[0].symbol_id is None


def test_touches_to_edges_anchor_only_touch_produces_file_edge() -> None:
    """不変条件1: 編集記録1件からは必ず1本以上のひも付けができる（行範囲が無くても）"""
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

    edges = touches_to_edges(
        touch, "ex1", "src/foo.py", [_symbol("a", line=1, end_line=5)]
    )

    assert len(edges) == 1
    assert edges[0].granularity == "file"


def test_touches_to_edges_file_only_touch_produces_file_edge() -> None:
    touch = CodeTouch(
        harness="claude",
        tool_call_id="call_1",
        file_path="/repo/src/foo.py",
        touch_kind="edit",
        locators=(FileOnly(),),
        added=0,
        removed=0,
        ts=None,
    )

    edges = touches_to_edges(touch, "ex1", "src/foo.py", [])

    assert len(edges) == 1
    assert edges[0].granularity == "file"
    assert edges[0].symbol_id is None


def test_touches_to_edges_empty_locators_still_produces_one_edge() -> None:
    """不変条件1の土台: 手がかりが1つも無くても最低1本は作る"""
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

    edges = touches_to_edges(touch, "ex1", "src/foo.py", [])

    assert len(edges) == 1


def test_touches_to_edges_multi_hunk_multiple_symbols_produces_multiple_edges() -> None:
    touch = _touch(
        LineRange(old_start=2, old_lines=1, new_start=2, new_lines=1),
        LineRange(old_start=20, old_lines=1, new_start=20, new_lines=1),
    )
    symbols = [_symbol("a", line=1, end_line=5), _symbol("b", line=18, end_line=22)]

    edges = touches_to_edges(touch, "ex1", "src/foo.py", symbols)

    assert len(edges) == 2
    assert {e.symbol_id for e in edges} == {
        sha256("src/foo.py:a"),
        sha256("src/foo.py:b"),
    }
    assert all(e.granularity == "line" for e in edges)


def test_touches_to_edges_edge_kind_matches_touch_kind() -> None:
    touch = _touch(
        LineRange(old_start=6, old_lines=1, new_start=6, new_lines=1),
        touch_kind="write",
    )
    edges = touches_to_edges(touch, "ex1", "src/foo.py", [])
    assert edges[0].edge_kind == "write"


def test_touches_to_edges_id_is_deterministic() -> None:
    touch = _touch(LineRange(old_start=6, old_lines=1, new_start=6, new_lines=1))
    symbols = [_symbol("greet", line=5, end_line=8)]

    edges_a = touches_to_edges(touch, "ex1", "src/foo.py", symbols)
    edges_b = touches_to_edges(touch, "ex1", "src/foo.py", symbols)

    assert edges_a[0].id == edges_b[0].id
