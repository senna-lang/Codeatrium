"""resolve_line_range / resolve_symbol_name のテスト（design §2.4・§5.3）

これらは編集記録から「どの関数を触ったか」を推定する純関数。
実測（design §2.4）どおり best-effort であり、分からなければ None を返す
（推測して間違った症状を出さないことが最優先 — §3.3）。
"""

from codeatrium.code_touches import resolve_line_range, resolve_symbol_name
from codeatrium.models import FileOnly, LineRange, TextAnchor

# ---- resolve_line_range ----


def test_resolve_line_range_uses_old_start_from_line_range() -> None:
    locators = (LineRange(old_start=10, old_lines=3, new_start=10, new_lines=5), FileOnly())
    result = resolve_line_range(locators, original_content=None)
    assert result == (10, 12)


def test_resolve_line_range_single_line_when_old_lines_is_one() -> None:
    locators = (LineRange(old_start=10, old_lines=1, new_start=10, new_lines=1),)
    result = resolve_line_range(locators, original_content=None)
    assert result == (10, 10)


def test_resolve_line_range_old_lines_none_treated_as_single_line() -> None:
    locators = (LineRange(old_start=10, old_lines=None, new_start=10, new_lines=2),)
    result = resolve_line_range(locators, original_content=None)
    assert result == (10, 10)


def test_resolve_line_range_skips_line_range_with_missing_old_start() -> None:
    """異常系: 行番号が欠けている LineRange は使えないので TextAnchor へフォールバック"""
    locators = (
        LineRange(old_start=None, old_lines=None, new_start=10, new_lines=1),
        TextAnchor(old_string="a\nb", new_string="c\nd"),
        FileOnly(),
    )
    original_content = "x\ny\na\nb\nz\n"
    result = resolve_line_range(locators, original_content)
    assert result == (3, 4)


def test_resolve_line_range_picks_first_usable_line_range_among_several() -> None:
    locators = (
        LineRange(old_start=None, old_lines=None, new_start=1, new_lines=1),
        LineRange(old_start=50, old_lines=2, new_start=51, new_lines=2),
    )
    result = resolve_line_range(locators, original_content=None)
    assert result == (50, 51)


def test_resolve_line_range_text_anchor_single_match() -> None:
    locators = (TextAnchor(old_string="target line\n", new_string="new line\n"),)
    original_content = "before\ntarget line\nafter\n"
    result = resolve_line_range(locators, original_content)
    assert result == (2, 2)


def test_resolve_line_range_text_anchor_multiline_match() -> None:
    locators = (TextAnchor(old_string="a\nb\nc", new_string="x"),)
    original_content = "head\na\nb\nc\ntail\n"
    result = resolve_line_range(locators, original_content)
    assert result == (2, 4)


def test_resolve_line_range_text_anchor_not_found_returns_none() -> None:
    locators = (TextAnchor(old_string="does not exist", new_string="x"),)
    original_content = "line1\nline2\n"
    assert resolve_line_range(locators, original_content) is None


def test_resolve_line_range_text_anchor_ambiguous_match_returns_none() -> None:
    """不変条件: 複数箇所にマッチしたら推測せず None（§3.3）"""
    locators = (TextAnchor(old_string="dup", new_string="x"),)
    original_content = "dup\nsomething\ndup\n"
    assert resolve_line_range(locators, original_content) is None


def test_resolve_line_range_text_anchor_none_old_string_returns_none() -> None:
    """Write の TextAnchor（old_string=None）は検索しようがない"""
    locators = (TextAnchor(old_string=None, new_string="print(1)\n"), FileOnly())
    assert resolve_line_range(locators, original_content="anything\n") is None


def test_resolve_line_range_text_anchor_without_original_content_returns_none() -> None:
    locators = (TextAnchor(old_string="a", new_string="b"),)
    assert resolve_line_range(locators, original_content=None) is None


def test_resolve_line_range_file_only_returns_none() -> None:
    assert resolve_line_range((FileOnly(),), original_content=None) is None


def test_resolve_line_range_empty_locators_returns_none() -> None:
    assert resolve_line_range((), original_content=None) is None


# ---- resolve_symbol_name ----

PY_CONTENT = (
    "import os\n"
    "\n"
    "\n"
    "def list_dir(path):\n"
    "    return os.listdir(path)\n"
)


def test_resolve_symbol_name_finds_enclosing_def_in_original_content() -> None:
    # line_range は old 座標（4行目の def 直後、5行目の return）
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(5, 5), patch_body=[], original_content=PY_CONTENT, lang=".py"
    )
    assert symbol_name == "list_dir"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_finds_class_in_original_content() -> None:
    content = "class Foo:\n    def bar(self):\n        pass\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(1, 1), patch_body=[], original_content=content, lang=".py"
    )
    assert symbol_name == "Foo"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_empty_original_content_falls_through_to_patch_body() -> None:
    """異常系: originalFile のキーはあるが中身が空（新規作成など）。'original_file' を騙らない"""
    patch_body = ["-def old_name():", "+def new_name():"]
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(1, 1), patch_body=patch_body, original_content="", lang=".py"
    )
    assert symbol_name == "new_name"
    assert resolved_by == "patch_body"


def test_resolve_symbol_name_none_original_content_falls_through_to_patch_body() -> None:
    patch_body = ["+def new_name():"]
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=None, patch_body=patch_body, original_content=None, lang=".py"
    )
    assert symbol_name == "new_name"
    assert resolved_by == "patch_body"


def test_resolve_symbol_name_line_range_none_skips_original_content_strategy() -> None:
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=None, patch_body=["+def from_patch():"], original_content=PY_CONTENT, lang=".py"
    )
    assert symbol_name == "from_patch"
    assert resolved_by == "patch_body"


def test_resolve_symbol_name_no_enclosing_declaration_falls_through_to_patch_body() -> None:
    """def/class の外（トップレベル文）を編集した場合は次の戦略へ"""
    content = "x = 1\ny = 2\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(2, 2), patch_body=["+def fallback():"], original_content=content, lang=".py"
    )
    assert symbol_name == "fallback"
    assert resolved_by == "patch_body"


def test_resolve_symbol_name_nothing_found_returns_none_none() -> None:
    """不変条件2の土台: 特定できなければ (None, None) を返しファイル粒度に落とす"""
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(1, 1), patch_body=["   context line", "-x = 1", "+x = 2"],
        original_content="x = 1\n", lang=".py",
    )
    assert symbol_name is None
    assert resolved_by is None


def test_resolve_symbol_name_strips_diff_prefix_before_matching() -> None:
    """patch_body の行は diff の +/-/space プレフィックス付きで渡ってくる"""
    patch_body = ["   def context_fn():", "-    old = 1", "+    new = 1"]
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=None, patch_body=patch_body, original_content=None, lang=".py"
    )
    assert symbol_name == "context_fn"
    assert resolved_by == "patch_body"


def test_resolve_symbol_name_typescript_function() -> None:
    content = "export function listDir(path: string): string[] {\n  return [];\n}\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(2, 2), patch_body=[], original_content=content, lang=".ts"
    )
    assert symbol_name == "listDir"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_typescript_arrow_const() -> None:
    content = "const listDir = (path: string) => {\n  return [];\n};\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(2, 2), patch_body=[], original_content=content, lang=".ts"
    )
    assert symbol_name == "listDir"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_go_func() -> None:
    content = "func ListDir(path string) []string {\n\treturn nil\n}\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(2, 2), patch_body=[], original_content=content, lang=".go"
    )
    assert symbol_name == "ListDir"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_does_not_attribute_sibling_code_to_earlier_def() -> None:
    """不変条件: 手前に def があっても、対象行が同じインデントの兄弟コードなら包含しない

    ナイーブに「手前で最初に見つかった def/class」を返す実装だと、モジュールレベルの
    定数を編集しただけで手前の関数の中だと誤判定する — 自信満々の間違い（design §3.3）。
    """
    content = "def helper():\n    pass\n\n\nCONSTANT = 1\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(5, 5), patch_body=[], original_content=content, lang=".py"
    )
    assert symbol_name is None
    assert resolved_by is None


def test_resolve_symbol_name_finds_correct_enclosing_def_among_siblings() -> None:
    """包含チェックが正しく機能する対照ケース: 対象行は second_fn の中"""
    content = "def first_fn():\n    pass\n\n\ndef second_fn():\n    return 1\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(6, 6), patch_body=[], original_content=content, lang=".py"
    )
    assert symbol_name == "second_fn"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_nested_function_finds_innermost() -> None:
    content = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    )
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(3, 3), patch_body=[], original_content=content, lang=".py"
    )
    assert symbol_name == "inner"
    assert resolved_by == "original_file"


def test_resolve_symbol_name_line_beyond_content_length_falls_through() -> None:
    """異常系: line_range が原文の行数を超えている（座標がズレている）。推測せず次戦略へ"""
    content = "x = 1\n"
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(99, 99), patch_body=["+def fallback():"], original_content=content, lang=".py"
    )
    assert symbol_name == "fallback"
    assert resolved_by == "patch_body"


def test_resolve_symbol_name_unsupported_lang_returns_none_none() -> None:
    symbol_name, resolved_by = resolve_symbol_name(
        line_range=(1, 1), patch_body=["+def fn():"], original_content="def fn():\n    pass\n", lang=".rb"
    )
    assert symbol_name is None
    assert resolved_by is None
