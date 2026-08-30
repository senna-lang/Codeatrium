"""grok の ACP tool_call から編集位置を CodeTouch へ正規化する。

grok は編集に行番号を一切持たない（`locations` は read_file 等にしか付かない）。
design §3.3 の分類どおり `anchor` capability で、位置の割り出しは core の文字列照合に委ねる。

実ログ39本の実測を反映した扱い:
  - ツールの識別は `name` ではなく `title`（`write` 320件 / `search_replace` 280件）。
  - **1つの `toolCallId` に対して更新が複数回届く**（`in_progress` と `completed` で
    同じ diff ブロックが2回来るのが 595/600）。エントリを素朴に列挙すると編集記録が
    二重になるため、`toolCallId` ごとに1件へまとめる。
  - **パスの正は `tool_call` の `rawInput` ではなく `tool_call_update` の diff ブロック**。
    `rawInput.file_path` は相対のことがあり（実測600件中60件）、diff 側は常に絶対パス。
    これを使えば cwd を引く必要がない。
  - 最終ステータスが `failed` の呼び出し（実測5件）は編集が適用されていないので記録しない。
  - `sessionUpdate` だけで判定し `method` では絞らない（`session/update` と
    `_x.ai/session/update` の2系統があり、将来どちらに移っても壊れないようにする）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from codeatrium.models import CodeLocator, CodeTouch, FileOnly, TextAnchor

# title でツールを識別する。値は touch_kind への対応表そのもの。
_EDIT_TOOL_KINDS: dict[str, str] = {"write": "write", "search_replace": "edit"}


@dataclass
class _CallState:
    """1つの toolCallId について、複数の更新から集めた最終状態。"""

    touch_kind: str
    raw_path: str | None = None
    raw_old: str | None = None
    raw_new: str | None = None
    diff_path: str | None = None
    diff_old: str | None = None
    diff_new: str | None = None
    status: str | None = None
    ts: str | None = None
    order: int = 0
    seen_paths: list[str] = field(default_factory=list)


def edit_capability() -> str:
    """grok は行番号を持たないため anchor（design §3.3）。"""
    return "anchor"


def extract_code_touches(raw_entries: list[dict[str, Any] | None]) -> list[CodeTouch]:
    """完了した write/search_replace を、toolCallId ごとに1件の編集記録として返す。"""
    calls = _collect_calls(raw_entries)

    touches: list[CodeTouch] = []
    for call_id, state in sorted(calls.items(), key=lambda item: item[1].order):
        # 適用されなかった編集は記録しない（間違ったひも付けより空が良い — §3.3）
        if state.status == "failed":
            continue
        file_path = state.diff_path or state.raw_path
        if not file_path:
            continue

        old_string = _non_empty(
            state.diff_old if state.diff_old is not None else state.raw_old
        )
        new_string = _non_empty(
            state.diff_new if state.diff_new is not None else state.raw_new
        )
        locators: list[CodeLocator] = []
        if old_string is not None or new_string is not None:
            locators.append(TextAnchor(old_string=old_string, new_string=new_string))
        locators.append(FileOnly())

        touches.append(
            CodeTouch(
                harness="grok",
                tool_call_id=call_id,
                file_path=file_path,
                touch_kind=state.touch_kind,  # type: ignore[arg-type]
                locators=tuple(locators),
                added=len(new_string.splitlines()) if new_string else 0,
                removed=len(old_string.splitlines()) if old_string else 0,
                ts=state.ts,
            )
        )

    return touches


def _collect_calls(
    raw_entries: list[dict[str, Any] | None],
) -> dict[str, _CallState]:
    """tool_call と後続の tool_call_update を toolCallId ごとに1件へ畳み込む。"""
    calls: dict[str, _CallState] = {}

    for order, entry in enumerate(raw_entries):
        update = _update_of(entry)
        if update is None:
            continue
        session_update = update.get("sessionUpdate")
        if session_update not in ("tool_call", "tool_call_update"):
            continue
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            continue

        state = calls.get(call_id)
        if state is None:
            touch_kind = _EDIT_TOOL_KINDS.get(str(update.get("title")))
            if touch_kind is None:
                # tool_call_update しか届かない編集は無い（実測）。編集以外はここで捨てる
                continue
            state = _CallState(
                touch_kind=touch_kind,
                ts=_normalize_ts(entry.get("timestamp") if isinstance(entry, dict) else None),
                order=order,
            )
            calls[call_id] = state

        _absorb_raw_input(state, update.get("rawInput"))
        _absorb_diff(state, update.get("content"))
        status = update.get("status")
        if isinstance(status, str):
            # 後から届いた確定ステータスで上書きする（in_progress → completed/failed）
            state.status = status

    return calls


def _update_of(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    params = entry.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    return update if isinstance(update, dict) else None


def _absorb_raw_input(state: _CallState, raw_input: Any) -> None:
    if not isinstance(raw_input, dict):
        return
    path = raw_input.get("file_path")
    if isinstance(path, str) and path:
        state.raw_path = path
    for key, attr in (("old_string", "raw_old"), ("new_string", "raw_new"), ("content", "raw_new")):
        value = raw_input.get(key)
        if isinstance(value, str) and getattr(state, attr) is None:
            setattr(state, attr, value)


def _absorb_diff(state: _CallState, content: Any) -> None:
    """diff ブロックを取り込む。同じ内容が複数回来るので上書きで畳み込む。"""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "diff":
            continue
        path = block.get("path")
        if isinstance(path, str) and path:
            state.diff_path = path
        old_text = block.get("oldText")
        new_text = block.get("newText")
        if isinstance(old_text, str):
            state.diff_old = old_text
        if isinstance(new_text, str):
            state.diff_new = new_text


def _normalize_ts(timestamp: Any) -> str | None:
    """grok の timestamp は整数 epoch 秒（実測819/819）。ISO 文字列へ揃える。

    他ハーネスの `code_touches.ts` は ISO 文字列なので、そのまま入れると
    並べ替えの基準が揃わない。文字列で来た場合はそのまま通す。
    """
    if isinstance(timestamp, str) and timestamp:
        return timestamp
    if isinstance(timestamp, int | float) and not isinstance(timestamp, bool):
        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
    return None


def _non_empty(value: str | None) -> str | None:
    """空文字は「何にも一致しない」と読めてしまうため None に寄せる。

    write の diff ブロックは `oldText: ""` を持つ（他ハーネスの write は None）。
    """
    return value if value else None
