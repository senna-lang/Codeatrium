"""codeatrium.utils の純関数に対する単体テスト"""

from __future__ import annotations

from codeatrium.utils import escape_like


def test_escape_like_escapes_percent_wildcard() -> None:
    assert escape_like("100%done") == r"100\%done"


def test_escape_like_escapes_underscore_wildcard() -> None:
    assert escape_like("release_1.0") == r"release\_1.0"


def test_escape_like_escapes_escape_char_itself() -> None:
    """エスケープ文字自身が値に含まれる場合、二重エスケープが必要"""
    assert escape_like("a\\b") == "a\\\\b"


def test_escape_like_escapes_escape_char_before_wildcards() -> None:
    """`\\%` は「エスケープ済み %」に化けてはならない ── まずエスケープ文字自身を
    二重化してから % を処理しないと `\\%` が literal `%` に誤読される"""
    assert escape_like("\\%") == "\\\\\\%"


def test_escape_like_leaves_plain_text_unchanged() -> None:
    assert escape_like("main") == "main"


def test_escape_like_handles_empty_string() -> None:
    assert escape_like("") == ""


def test_escape_like_supports_custom_escape_char() -> None:
    assert escape_like("100%", escape_char="!") == "100!%"
