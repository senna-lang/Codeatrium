"""LLM 応答から JSON 本体を頑健に取り出すユーティリティ。

open / hosted model は JSON を ```json ... ``` フェンスで包む / 前後に散文
（検証ノート等）を付ける / reasoning 系は <think>...</think> の思考ブロックを
先頭に付け、その中に中間 JSON を書く。これらに対し strict な json.loads の
前段で「think 除去 → フェンス採用(最後) → 最後の balanced JSON 値抽出」を行う
純関数を提供する。最終回答は末尾に来る前提で「最後の値」を採るため、思考中の
中間 JSON を誤採用しない。

`llm._call_openai` がローカル/ホスト双方の応答パースに使用する。
"""

from __future__ import annotations

import re

# ```json ... ``` または ``` ... ``` のフェンスブロックを捕捉する。
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
# reasoning モデルの思考ブロック (中に括弧を含み得る) を除去する。
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _all_balanced_values(s: str) -> list[str]:
    """s 中のトップレベルの balanced な JSON 値 ({...}/[...]) を出現順に全て返す。

    文字列・エスケープを考慮し、文字列内の括弧は数えない。
    """
    opens = {"{": "}", "[": "]"}
    values: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch not in opens:
            i += 1
            continue
        open_ch = ch
        close_ch = opens[open_ch]
        depth = 0
        in_str = False
        escape = False
        j = i
        end = -1
        while j < n:
            c = s[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end == -1:
            break  # 閉じない → これ以降は不完全。打ち切る。
        values.append(s[i : end + 1])
        i = end + 1
    return values


def extract_json(text: str) -> str:
    """LLM 応答テキストから JSON 文字列部分を取り出して返す。

    <think> 除去 → フェンス採用(最後) → 最後の balanced JSON 値 の順で頑健化する。
    最終回答は末尾に来る前提。json.loads は呼び出し側で行う。
    """
    s = text.strip()

    # (1) reasoning の思考ブロックを除去 (中の中間 JSON を誤採用しないため)
    s = _THINK.sub(" ", s).strip()

    # (2) markdown フェンスがあれば最後のフェンス内容を採用 (思考後の最終回答が末尾フェンスに来る)
    fences = _FENCE.findall(s)
    if fences:
        s = fences[-1].strip()

    # (3) balanced な JSON 値のうち最後のものを採用 (末尾の最終回答を優先)
    values = _all_balanced_values(s)
    if values:
        return values[-1]

    return s
