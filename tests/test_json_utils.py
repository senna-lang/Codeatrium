"""json_utils.extract_json の単体テスト。

フェンス / 散文 / <think> / 複数 JSON から最終値だけを取り出す契約を固定する。
"""

from __future__ import annotations

import json

import pytest

from codeatrium.json_utils import extract_json

_PALACE = {
    "exchange_core": "core",
    "specific_context": "ctx",
    "room_assignments": [
        {
            "room_type": "concept",
            "room_key": "k",
            "room_label": "l",
            "relevance": 0.9,
        }
    ],
}


def test_extract_json_plain() -> None:
    """素の JSON はそのまま返る。"""
    raw = json.dumps(_PALACE)
    assert json.loads(extract_json(raw)) == _PALACE


def test_extract_json_fenced() -> None:
    """```json フェンスを外して本体を返す。"""
    raw = f"```json\n{json.dumps(_PALACE)}\n```"
    assert json.loads(extract_json(raw)) == _PALACE


def test_extract_json_prose_around_fence() -> None:
    """前後の散文付きフェンスから本体を返す。"""
    raw = f"結果です:\n```json\n{json.dumps(_PALACE)}\n```\n以上。"
    assert json.loads(extract_json(raw)) == _PALACE


def test_extract_json_strips_think_block() -> None:
    """<think> 内の中間 JSON を捨て、末尾の最終 JSON を採る。"""
    intermediate = json.dumps({"scratch": True})
    final = json.dumps(_PALACE)
    raw = f"<think>\n途中案: {intermediate}\n</think>\n{final}"
    assert json.loads(extract_json(raw)) == _PALACE


def test_extract_json_prefers_last_balanced_value() -> None:
    """複数の balanced JSON がある場合は最後を採る。"""
    first = json.dumps({"draft": 1})
    second = json.dumps(_PALACE)
    raw = f"note {first}\nfinal {second}"
    assert json.loads(extract_json(raw)) == _PALACE


def test_extract_json_array_value() -> None:
    """トップレベル配列も balanced 抽出できる。"""
    raw = 'prefix [{"a": 1}] suffix'
    assert json.loads(extract_json(raw)) == [{"a": 1}]


def test_extract_json_unbalanced_falls_back_to_stripped() -> None:
    """閉じ括弧が無い入力は strip 済み原文を返す（json.loads は呼び出し側）。"""
    raw = '  {"broken": true  '
    assert extract_json(raw) == '{"broken": true'


@pytest.mark.parametrize(
    "raw",
    [
        f"```\n{json.dumps(_PALACE)}\n```",
        f"```JSON\n{json.dumps(_PALACE)}\n```",
    ],
)
def test_extract_json_fence_language_variants(raw: str) -> None:
    """言語タグなし / 大文字 JSON のフェンスも扱う。"""
    assert json.loads(extract_json(raw)) == _PALACE
