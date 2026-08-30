"""omp-pi の toolCall から編集位置を CodeTouch へ正規化する。

omp のパッチ形式は公開仕様のない独自 DSL（`SWAP n.=m:` / `INS.HEAD:` / `PUT` / `DEL` など）で、
行番号の解釈を誤ると静かにズレる。design §3.3 の決定どおり **DSL は解読せず**、
パッチ本文をそのまま `TextAnchor.new_string` として使う（`edit_capability` は `anchor`）。

実ログ99本の実測を反映した扱い:
  - **パスの正は toolCall 側ではなく toolResult 側**。toolCall のヘッダは入れ子の作業
    ディレクトリ基準に切り詰められていることがあり（実測 1014件中 95件が不一致、
    すべて basename は同じでプレフィックスだけが欠けている）、そのまま cwd と結合すると
    存在しないパスへひも付けてしまう。`toolCallId` で対応する toolResult の
    `[path#hash]` ヘッダを優先する（Claude の `toolUseResult` と同じ構図）。
  - toolResult が無い／ヘッダ数が食い違う場合だけ toolCall 側のヘッダへ退避する。
  - `*** Begin Patch` が前置きされる場合はその次行がヘッダになる。
  - 1回の toolCall が複数ファイルを含むことがある（実測で最大6ヘッダ）。ヘッダ単位に
    分割し、ファイルごとに1つの CodeTouch を作る（本文もそのファイルの区間だけを持たせる）。
  - パスの大半は相対（edit 323/343・write 475/568）。相対のままだと不変条件3の判定で
    落ちるため、session エントリの cwd を entry に載せてもらい、ここで絶対パス化する。
  - `edits` 配列（`old_text`/`new_text`）を持つ形もあり、こちらは TextAnchor へ直接写せる。
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from codeatrium.models import CodeLocator, CodeTouch, FileOnly, TextAnchor

_EDIT_TOOL_NAMES = frozenset({"edit", "write"})

# パッチ本文のファイルヘッダ。`[path#hash]` の hash は省略されることがある。
# toolResult には JSON 配列がそのまま出力されることがあり（MCP ツールの戻り値など）、
# `[{...}]` の1行が緩いパターンだとヘッダとして誤マッチする。パスに現れない
# `{` `}` `"` を除外してファイルパスらしい形だけを拾う。
_HEADER_RE = re.compile(r"^\[([^\]#{}\"]+)(?:#[^\]]*)?\]$")

# ファイル移動コマンド。行番号を伴わないので誤解読のリスクが無く、§8.2 段1 の対象になる。
# 緩いマッチは誤検出につながるため、この形以外は拾わない。
_MOVE_RE = re.compile(r"^MV\s+(\S+)\s+->\s+(\S+)\s*$")

# omp は MCP ツール呼び出しなど、ファイルではない書き込み先を URI スキームで表す
# （実測 `xd://mcp__serena_...` が326件）。cwd と結合するとリポジトリ内の実在しない
# パスに化けてしまうため、スキーム付きは編集記録として扱わない。
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def edit_capability() -> str:
    """omp-pi は独自 DSL を解読しないため anchor に留める（design §3.3）。"""
    return "anchor"


def extract_code_touches(raw_entries: list[dict[str, Any] | None]) -> list[CodeTouch]:
    """assistant の edit/write toolCall を、ファイル単位の編集記録として返す。"""
    touches: list[CodeTouch] = []
    resolved = _resolved_paths_by_call(raw_entries)

    for entry, block in _iter_tool_calls(raw_entries):
        cwd = entry.get("cwd")
        timestamp = entry.get("timestamp")
        ts = timestamp if isinstance(timestamp, str) else None
        call_id = _call_id(block, entry)
        if call_id is None:
            continue

        tool_name = block.get("name")
        arguments = block.get("arguments")
        if not isinstance(arguments, dict):
            continue

        result_paths = resolved.get(call_id, [])
        if tool_name == "write":
            touch = _write_touch(call_id, arguments, result_paths, cwd, ts)
            if touch is not None:
                touches.append(touch)
            continue

        edits = arguments.get("edits")
        if isinstance(edits, list):
            touches.extend(
                _edits_touches(call_id, arguments, edits, result_paths, cwd, ts)
            )
            continue

        touches.extend(_patch_touches(call_id, arguments, result_paths, cwd, ts))

    return touches


def _resolved_paths_by_call(
    raw_entries: list[dict[str, Any] | None],
) -> dict[str, list[str]]:
    """toolResult 本文の `[path#hash]` ヘッダを toolCallId ごとに集める。

    omp は編集を適用した「実際の」パスを toolResult 側に書き戻す。toolCall 側のヘッダは
    入れ子ディレクトリ基準に切り詰められることがあるため、こちらを正として扱う。
    """
    resolved: dict[str, list[str]] = {}

    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "toolResult":
            continue
        call_id = message.get("toolCallId")
        content = message.get("content")
        if not isinstance(call_id, str) or not isinstance(content, list):
            continue

        text = "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        paths = [
            match.group(1)
            for line in text.splitlines()
            if (match := _HEADER_RE.match(line)) is not None
        ]
        if paths:
            resolved[call_id] = paths

    return resolved


def _prefer_resolved(call_paths: list[str], result_paths: list[str]) -> list[str]:
    """toolResult 側のパスを優先する。件数が食い違うときだけ toolCall 側へ退避する。

    件数が一致すれば順番どおりに対応する（複数ファイルパッチでも実測で順序は保たれる）。
    食い違う場合に無理に対応付けると別ファイルへひも付けかねないため、推測しない。
    """
    if len(result_paths) == len(call_paths):
        return result_paths
    return call_paths


def extract_file_renames(
    raw_entries: list[dict[str, Any] | None],
) -> list[tuple[str, str, str | None]]:
    """パッチ本文の `MV 旧 -> 新` を旧新パスと時刻で返す（design §8.2 段1）。

    DSL 全体は解読しないが、この1行だけは行番号を含まないため誤解読の危険が無い。
    取りこぼしても「改名を追えない」だけで、誤った行を指すことはない。

    MV のパスはパッチヘッダと同じ基準で書かれるため、ヘッダが切り詰められていた場合は
    同じだけ欠けている。toolResult 側の解決済みパスから接頭辞を復元して補う。
    """
    renames: list[tuple[str, str, str | None]] = []
    resolved = _resolved_paths_by_call(raw_entries)

    for entry, block in _iter_tool_calls(raw_entries):
        if block.get("name") != "edit":
            continue
        arguments = block.get("arguments")
        if not isinstance(arguments, dict):
            continue
        patch = arguments.get("input")
        if not isinstance(patch, str):
            continue

        cwd = entry.get("cwd")
        timestamp = entry.get("timestamp")
        ts = timestamp if isinstance(timestamp, str) else None
        call_id = _call_id(block, entry)
        prefix = _missing_prefix(
            [path for path, _ in _split_patch_by_file(patch)],
            resolved.get(call_id, []) if call_id else [],
        )
        for line in patch.splitlines():
            match = _MOVE_RE.match(line)
            if match is None:
                continue
            old_path = _absolutize(prefix + match.group(1), cwd)
            new_path = _absolutize(prefix + match.group(2), cwd)
            if old_path and new_path and old_path != new_path:
                renames.append((old_path, new_path, ts))

    return renames


def _missing_prefix(call_paths: list[str], result_paths: list[str]) -> str:
    """toolCall 側のヘッダから欠けているディレクトリ接頭辞を復元する。

    解決済みパスが toolCall 側のパスを末尾に含む場合、その差分が欠けた接頭辞になる。
    復元できないときは空文字を返し、cwd 基準の従来どおりの解釈に任せる。
    """
    if len(call_paths) != len(result_paths):
        return ""
    for call_path, result_path in zip(call_paths, result_paths, strict=True):
        if result_path == call_path:
            return ""
        if result_path.endswith("/" + call_path):
            return result_path[: -len(call_path)]
    return ""


def _iter_tool_calls(raw_entries: list[dict[str, Any] | None]):
    """assistant メッセージ内の edit/write toolCall を (entry, block) で列挙する。"""
    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            if block.get("name") not in _EDIT_TOOL_NAMES:
                continue
            yield entry, block


def _call_id(block: dict[str, Any], entry: dict[str, Any]) -> str | None:
    call_id = block.get("id")
    if isinstance(call_id, str) and call_id:
        return call_id
    entry_id = entry.get("id")
    return entry_id if isinstance(entry_id, str) and entry_id else None


def _absolutize(path: str, cwd: Any) -> str | None:
    """相対パスを session の cwd で絶対化する。cwd が無ければ相対のまま返す。

    相対のまま返した場合は normalize_repo_path が不変条件3で落とす（勝手に補わない）。
    ファイルを指さない URI（`xd://...`）は None を返して記録対象から外す。
    """
    if not path or _URI_SCHEME_RE.match(path):
        return None
    if path.startswith("/"):
        return path
    if isinstance(cwd, str) and cwd.startswith("/"):
        return posixpath.normpath(posixpath.join(cwd, path))
    return path


def _write_touch(
    call_id: str,
    arguments: dict[str, Any],
    result_paths: list[str],
    cwd: Any,
    ts: str | None,
) -> CodeTouch | None:
    path = arguments.get("path")
    if not isinstance(path, str):
        return None
    resolved = _prefer_resolved([path], result_paths)
    file_path = _absolutize(resolved[0], cwd)
    if file_path is None:
        return None

    content = arguments.get("content")
    locators: list[CodeLocator] = []
    added = 0
    if isinstance(content, str):
        locators.append(TextAnchor(old_string=None, new_string=content))
        added = len(content.splitlines())
    locators.append(FileOnly())

    return CodeTouch(
        harness="omp-pi",
        tool_call_id=call_id,
        file_path=file_path,
        touch_kind="write",
        locators=tuple(locators),
        added=added,
        removed=0,
        ts=ts,
    )


def _edits_touches(
    call_id: str,
    arguments: dict[str, Any],
    edits: list[Any],
    result_paths: list[str],
    cwd: Any,
    ts: str | None,
) -> list[CodeTouch]:
    """`edits` 配列は1要素ごとに1 touch にする（アンカー文字列を取りこぼさないため）。"""
    path = arguments.get("path")
    if not isinstance(path, str):
        return []
    resolved = _prefer_resolved([path], result_paths)
    file_path = _absolutize(resolved[0], cwd)
    if file_path is None:
        return []

    touches: list[CodeTouch] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            continue
        old_text = edit.get("old_text")
        new_text = edit.get("new_text")
        if not isinstance(old_text, str) and not isinstance(new_text, str):
            continue
        touches.append(
            CodeTouch(
                harness="omp-pi",
                tool_call_id=f"{call_id}:{index}",
                file_path=file_path,
                touch_kind="edit",
                locators=(
                    TextAnchor(
                        old_string=old_text if isinstance(old_text, str) else None,
                        new_string=new_text if isinstance(new_text, str) else None,
                    ),
                    FileOnly(),
                ),
                added=len(new_text.splitlines()) if isinstance(new_text, str) else 0,
                removed=len(old_text.splitlines()) if isinstance(old_text, str) else 0,
                ts=ts,
            )
        )
    return touches


def _patch_touches(
    call_id: str,
    arguments: dict[str, Any],
    result_paths: list[str],
    cwd: Any,
    ts: str | None,
) -> list[CodeTouch]:
    """独自 DSL のパッチ本文を、`[path#hash]` ヘッダ単位の touch へ分割する。"""
    patch = arguments.get("input")
    if not isinstance(patch, str):
        return []

    segments = _split_patch_by_file(patch)
    if not segments:
        # ヘッダが無いパッチ（実測12件）。arguments.path があればそれを使い、
        # 無ければ場所が特定できないので記録しない（推測しない — §3.3）。
        fallback = arguments.get("path")
        if not isinstance(fallback, str):
            return []
        segments = [(fallback, patch)]

    paths = _prefer_resolved([path for path, _ in segments], result_paths)

    touches: list[CodeTouch] = []
    for path, (_, body) in zip(paths, segments, strict=True):
        file_path = _absolutize(path, cwd)
        if file_path is None:
            continue
        added = sum(1 for line in body.splitlines() if line.startswith("+"))
        removed = sum(1 for line in body.splitlines() if line.startswith("-"))
        touches.append(
            CodeTouch(
                harness="omp-pi",
                # 1 toolCall が複数ファイルを含むため、ファイル名まで含めて一意にする
                tool_call_id=f"{call_id}:{path}",
                file_path=file_path,
                touch_kind="edit",
                locators=(
                    TextAnchor(old_string=None, new_string=body),
                    FileOnly(),
                ),
                added=added,
                removed=removed,
                ts=ts,
            )
        )
    return touches


def _split_patch_by_file(patch: str) -> list[tuple[str, str]]:
    """`[path#hash]` ヘッダごとにパッチ本文を切り分ける。

    本文を丸ごと各ファイルへ複製すると、あとで文字列照合するときに使い物にならず
    added も水増しになるため、そのファイルの区間だけを持たせる。
    """
    segments: list[tuple[str, list[str]]] = []
    for line in patch.splitlines():
        match = _HEADER_RE.match(line)
        if match is not None:
            segments.append((match.group(1), []))
        elif segments:
            segments[-1][1].append(line)
    return [(path, "\n".join(body)) for path, body in segments]
