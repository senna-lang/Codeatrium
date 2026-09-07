"""
.jsonl パース・exchange 分割・DB 保存

exchange 境界定義:
  role="user" かつ isMeta!=true かつ実質的なテキスト発話を持つエントリから
  次の同様エントリの直前まで。ツール呼び出し・中間応答は同一 exchange に含める。

フィルタルール（SPEC Section 6 / 論文 Section 3.1 準拠）:
  - 50文字未満の exchange は trivial として除外
  - isMeta=True の user エントリは exchange 境界としない

project_root を渡すと、exchange と同じコミットで code_touches / code_symbols / code_edges
（design §4.1・§5.5）も記録する。symbol 解決は tree-sitter でその場でディスクを読んで行う
（distill を待たない — §8.1 の不変条件を蒸留のスキップ条件から独立させるため）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from codeatrium.adapters.harness import claude as claude_adapter
from codeatrium.adapters.harness import codex as codex_adapter
from codeatrium.adapters.harness import grok as grok_adapter
from codeatrium.adapters.harness import omp_pi as omp_pi_adapter
from codeatrium.adapters.harness import opencode as opencode_adapter
from codeatrium.code_touches import (
    is_external_path,
)
from codeatrium.utils import sha256

if TYPE_CHECKING:
    pass


@dataclass
class Exchange:
    """exchange 単位の verbatim テキスト"""

    id: str
    conversation_id: str
    ply_start: int
    ply_end: int
    user_content: str
    agent_content: str
    files: list[str] = field(default_factory=list)
    git_branch: str | None = None


# ---- 内部ヘルパー ----


def _extract_tool_use_files(entries: list[dict | None]) -> list[str]:
    """
    assistant エントリから tool_use ブロックの file_path を抽出する。
    外部パスは除外し、重複をトリムしたリストを返す（順序保持）。
    """
    seen: set[str] = set()
    result: list[str] = []

    for entry in entries:
        if entry is None:
            continue
        if entry.get("type") != "assistant":
            continue

        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue

            name = block.get("name")
            if name not in {"Edit", "Write", "Read", "MultiEdit", "NotebookEdit"}:
                continue

            input_dict = block.get("input")
            if not isinstance(input_dict, dict):
                continue

            # NotebookEdit の場合は notebook_path、その他は file_path
            path = None
            if name == "NotebookEdit":
                path = input_dict.get("notebook_path")
            else:
                path = input_dict.get("file_path")

            if not path or not isinstance(path, str):
                continue

            # 外部パスはスキップ
            if is_external_path(path):
                continue

            # 重複排除（順序保持）
            if path not in seen:
                seen.add(path)
                result.append(path)

    return result


def _extract_text(content: Any) -> str:
    """message.content から平文テキストを抽出する"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "thinking":
                    pass  # thinking block は含めない
        return "\n".join(p for p in parts if p)
    return ""


# コンパクション要約の先頭パターン（CC が自動生成するセッション引き継ぎテキスト）
_COMPACT_PREFIXES = (
    "This session is being continued from a previous conversation",
    "前のセッションからの引き継ぎです",
    "このセッションは、以前の会話から引き継がれています",
)

# loci distill が claude --print に渡す蒸留プロンプトの先頭パターン
_DISTILL_PROMPT_PREFIX = "この対話のやり取りをJSONに蒸留してください"


def _is_compaction_summary(text: str) -> bool:
    """CC のコンパクション要約エントリか判定する"""
    t = text.strip()
    return any(t.startswith(prefix) for prefix in _COMPACT_PREFIXES)


def _is_real_user_entry(entry: dict) -> bool:
    """実質的なユーザー発話を持つ user エントリか判定する"""
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta", False):
        return False
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return False
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    text = _extract_text(content)
    # tool_result のみの場合は実質発話なし
    if isinstance(content, list) and all(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
        if isinstance(b, dict)
    ):
        return False
    # コンパクション要約は exchange 境界としない
    if _is_compaction_summary(text):
        return False
    # loci distill の蒸留プロンプトは除外
    if text.strip().startswith(_DISTILL_PROMPT_PREFIX):
        return False
    return bool(text.strip())


# ---- 公開API ----


def _load_raw_entries(jsonl_path: Path, last_ply_end: int) -> list[dict | None]:
    """.jsonl を1行ずつ読んで raw entry のリストを返す。
    last_ply_end 以前の行は None プレースホルダに置き換える（既インデックス領域の再構築を避ける）。
    parse_exchanges と code_touches 抽出（index_file）の両方から同じ ply 座標系で参照するための共有ローダー。
    """
    raw_entries: list[dict | None] = []
    if not jsonl_path.exists():
        return raw_entries
    ply = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if ply <= last_ply_end:
                # 既インデックス領域: 古い exchange は再構築しない（None プレースホルダ）。
                # ただし malformed 行は位置に数えない — last_ply_end は成功パース行のみを
                # 数えた座標系なので、検証パースして同じ座標系を維持する。
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_entries.append(None)
                ply += 1
            else:
                try:
                    raw_entries.append(json.loads(line))
                    ply += 1
                except json.JSONDecodeError:
                    continue
    return raw_entries


def parse_exchanges(
    jsonl_path: Path,
    min_chars: int = 50,
    last_ply_end: int = -1,
    raw_entries: list[dict | None] | None = None,
) -> list[Exchange]:
    """
    .jsonl ファイルを読んで exchange リストを返す。
    trivial（min_chars 文字未満）は除外する。
    last_ply_end: ply インデックス以前の行はスキップ（デフォルト -1 = 全行パース、既にインデックスされた部分の再処理を避けるため）。
    raw_entries: 呼び出し側が既に読み込み済みなら渡せる（index_file が code_touches 抽出と共有するため）。
    """
    if raw_entries is None:
        if not jsonl_path.exists():
            return []
        raw_entries = _load_raw_entries(jsonl_path, last_ply_end)

    conversation_id = sha256(str(jsonl_path))

    # exchange の境界インデックスを収集
    boundaries: list[int] = [
        i for i, e in enumerate(raw_entries) if e is not None and _is_real_user_entry(e)
    ]

    exchanges: list[Exchange] = []
    for b_idx, start in enumerate(boundaries):
        end = (
            boundaries[b_idx + 1] - 1
            if b_idx + 1 < len(boundaries)
            else len(raw_entries) - 1
        )

        user_entry = raw_entries[start]
        if user_entry is None:
            continue
        user_text = _extract_text(user_entry["message"]["content"])

        # assistant の発話を連結（コンパクション要約ゾーンは除外）
        agent_parts: list[str] = []
        in_compaction_zone = False
        for e in raw_entries[start + 1 : end + 1]:
            if e is None:
                continue
            if e.get("type") == "user":
                msg = e.get("message", {})
                if isinstance(msg, dict):
                    text = _extract_text(msg.get("content", ""))
                    in_compaction_zone = _is_compaction_summary(text)
                continue
            if e.get("type") == "assistant" and not in_compaction_zone:
                msg = e.get("message", {})
                if isinstance(msg, dict):
                    text = _extract_text(msg.get("content", ""))
                    if text:
                        agent_parts.append(text)

        agent_text = "\n".join(agent_parts)
        combined = user_text + agent_text

        # trivial フィルタ
        if len(combined) < min_chars:
            continue

        user_uuid = user_entry.get("uuid", f"{start}")
        git_branch_raw = user_entry.get("gitBranch", "")
        git_branch = (
            git_branch_raw
            if isinstance(git_branch_raw, str) and git_branch_raw.strip()
            else None
        )
        exchange_id = sha256(f"{conversation_id}:{user_uuid}")

        # tool_use から file パスを抽出
        tool_files = _extract_tool_use_files(raw_entries[start : end + 1])

        exchanges.append(
            Exchange(
                id=exchange_id,
                conversation_id=conversation_id,
                ply_start=start,
                ply_end=end,
                user_content=user_text,
                agent_content=agent_text,
                files=tool_files,
                git_branch=git_branch,
            )
        )

    return exchanges


def parse_codex_exchanges(
    jsonl_path: Path,
    min_chars: int = 50,
    last_ply_end: int = -1,
    raw_entries: list[dict | None] | None = None,
) -> list[Exchange]:
    """Codex rollout JSONL をユーザーターン単位の exchange に分割する。"""
    if raw_entries is None:
        if not jsonl_path.exists():
            return []
        raw_entries = _load_raw_entries(jsonl_path, last_ply_end)

    conversation_id = sha256(str(jsonl_path))
    boundaries = [
        index
        for index, entry in enumerate(raw_entries)
        if _is_codex_user_message(entry)
    ]

    exchanges: list[Exchange] = []
    for boundary_index, start in enumerate(boundaries):
        end = (
            boundaries[boundary_index + 1] - 1
            if boundary_index + 1 < len(boundaries)
            else len(raw_entries) - 1
        )
        user_entry = raw_entries[start]
        if user_entry is None:
            continue
        user_text = _codex_message_text(user_entry, "input_text")
        agent_text = "\n".join(
            _codex_message_text(entry, "output_text")
            for entry in raw_entries[start + 1 : end + 1]
            if isinstance(entry, dict) and _is_codex_assistant_message(entry)
        )
        if len(user_text + agent_text) < min_chars:
            continue

        turn_id = _codex_turn_id(raw_entries, start) or f"ply:{start}"
        touch_slice = raw_entries[start : end + 1]
        touches = codex_adapter.extract_code_touches(touch_slice)
        files = list(dict.fromkeys(touch.file_path for touch in touches))
        exchanges.append(
            Exchange(
                id=sha256(f"{conversation_id}:{turn_id}"),
                conversation_id=conversation_id,
                ply_start=start,
                ply_end=end,
                user_content=user_text,
                agent_content=agent_text,
                files=files,
                git_branch=_codex_git_branch(raw_entries, start),
            )
        )

    return exchanges


def _is_codex_user_message(entry: dict | None) -> bool:
    return _is_codex_message(entry, "user")


def _is_codex_assistant_message(entry: dict | None) -> bool:
    return _is_codex_message(entry, "assistant")


def _is_codex_message(entry: dict | None, role: str) -> bool:
    if not isinstance(entry, dict) or entry.get("type") != "response_item":
        return False
    payload = entry.get("payload")
    return (
        isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == role
    )


def _codex_message_text(entry: dict, content_type: str) -> str:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") == content_type
        and isinstance(part.get("text"), str)
    )


def _codex_turn_id(raw_entries: list[dict | None], start: int) -> str | None:
    for entry in reversed(raw_entries[: start + 1]):
        if not isinstance(entry, dict) or entry.get("type") != "turn_context":
            continue
        payload = entry.get("payload")
        turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def _codex_git_branch(raw_entries: list[dict | None], start: int) -> str | None:
    for entry in reversed(raw_entries[: start + 1]):
        if not isinstance(entry, dict) or entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        git = payload.get("git")
        branch = git.get("branch") if isinstance(git, dict) else None
        if isinstance(branch, str) and branch.strip():
            return branch
    return None


def parse_grok_exchanges(
    jsonl_path: Path,
    min_chars: int = 50,
    last_ply_end: int = -1,
    raw_entries: list[dict | None] | None = None,
) -> list[Exchange]:
    """grok の ACP セッション JSONL をユーザー発話単位の exchange に分割する。

    envelope は `{timestamp, method, params: {sessionId, update}}`。会話は
    `user_message_chunk` / `agent_message_chunk` で届く（実測ではどちらも1エントリに
    1メッセージが収まり、連続分割はされない）。`agent_thought_chunk` は思考なので
    agent_content に含めない。`hook_execution` など他の sessionUpdate は無視する。
    """
    if raw_entries is None:
        if not jsonl_path.exists():
            return []
        raw_entries = _load_raw_entries(jsonl_path, last_ply_end)

    conversation_id = sha256(str(jsonl_path))
    boundaries = [
        index
        for index, entry in enumerate(raw_entries)
        if _grok_chunk_text(entry, "user_message_chunk") is not None
    ]

    exchanges: list[Exchange] = []
    for boundary_index, start in enumerate(boundaries):
        end = (
            boundaries[boundary_index + 1] - 1
            if boundary_index + 1 < len(boundaries)
            else len(raw_entries) - 1
        )
        user_text = _grok_chunk_text(raw_entries[start], "user_message_chunk") or ""
        agent_text = "\n".join(
            text
            for entry in raw_entries[start + 1 : end + 1]
            if (text := _grok_chunk_text(entry, "agent_message_chunk")) is not None
        )
        if len(user_text + agent_text) < min_chars:
            continue

        touch_slice = raw_entries[start : end + 1]
        touches = grok_adapter.extract_code_touches(touch_slice)
        files = list(dict.fromkeys(touch.file_path for touch in touches))
        exchanges.append(
            Exchange(
                id=sha256(f"{conversation_id}:{start}"),
                conversation_id=conversation_id,
                ply_start=start,
                ply_end=end,
                user_content=user_text,
                agent_content=agent_text,
                files=files,
                # grok の ACP envelope (`session/update`) には git ブランチが一切載らない
                # (tests/fixtures/harness_logs/README.md の実ログ39本再調査で確認済み。
                # session/update・tool_call・tool_call_update いずれの params にも
                # git 関連フィールドは存在しない)。claude の gitBranch・codex の
                # session_meta.git.branch に相当するデータがそもそも無いため、
                # 実在しないフィールドを捏造せず None のままにする（issue #19）。
                git_branch=None,
            )
        )

    return exchanges


def _grok_chunk_text(entry: dict | None, session_update: str) -> str | None:
    """指定 sessionUpdate のチャンク本文を返す。該当しなければ None。"""
    if not isinstance(entry, dict):
        return None
    params = entry.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != session_update:
        return None
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return None
    text = content.get("text")
    return text if isinstance(text, str) else None


def parse_omp_pi_exchanges(
    jsonl_path: Path,
    min_chars: int = 50,
    last_ply_end: int = -1,
    raw_entries: list[dict | None] | None = None,
) -> list[Exchange]:
    """omp-pi のセッション JSONL をユーザー発話単位の exchange に分割する。

    envelope は `{type, id, parentId, timestamp, message}`。role は user / assistant に加えて
    toolResult / developer があり、message 以外の type（custom が実測 6553件）も混ざるため、
    exchange 境界は `type=="message"` かつ `role=="user"` に限定する。
    """
    if raw_entries is None:
        if not jsonl_path.exists():
            return []
        raw_entries = _load_raw_entries(jsonl_path, last_ply_end)

    conversation_id = sha256(str(jsonl_path))
    boundaries = [
        index
        for index, entry in enumerate(raw_entries)
        if _is_omp_pi_message(entry, "user")
    ]

    exchanges: list[Exchange] = []
    for boundary_index, start in enumerate(boundaries):
        end = (
            boundaries[boundary_index + 1] - 1
            if boundary_index + 1 < len(boundaries)
            else len(raw_entries) - 1
        )
        user_entry = raw_entries[start]
        if user_entry is None:
            continue

        user_text = _omp_pi_text(user_entry)
        agent_text = "\n".join(
            text
            for entry in raw_entries[start + 1 : end + 1]
            if isinstance(entry, dict)
            and _is_omp_pi_message(entry, "assistant")
            and (text := _omp_pi_text(entry))
        )
        if len(user_text + agent_text) < min_chars:
            continue

        touch_slice = raw_entries[start : end + 1]
        touches = omp_pi_adapter.extract_code_touches(touch_slice)
        files = list(dict.fromkeys(touch.file_path for touch in touches))
        exchanges.append(
            Exchange(
                id=sha256(f"{conversation_id}:{user_entry.get('id', start)}"),
                conversation_id=conversation_id,
                ply_start=start,
                ply_end=end,
                user_content=user_text,
                agent_content=agent_text,
                files=files,
                # omp-pi のセッション envelope には `{type: "session", cwd}` の
                # 作業ディレクトリしか無く、git ブランチは記録されない
                # (tests/fixtures/harness_logs/README.md の実ログ99本再調査で確認済み)。
                # cwd から index 時点のブランチを別途 git 問い合わせすることは、
                # 発話当時のブランチではなく現在のブランチを記録してしまい claude/codex の
                # 意味と食い違うため行わない。実在しないフィールドを捏造せず None のままに
                # する（issue #19）。
                git_branch=None,
            )
        )

    return exchanges


def _is_omp_pi_message(entry: dict | None, role: str) -> bool:
    if not isinstance(entry, dict) or entry.get("type") != "message":
        return False
    message = entry.get("message")
    return isinstance(message, dict) and message.get("role") == role


def _omp_pi_text(entry: dict) -> str:
    """message.content の text ブロックだけを連結する（thinking / toolCall は含めない）。"""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _annotate_omp_pi_cwd(jsonl_path: Path, raw_entries: list[dict | None]) -> None:
    """session エントリの cwd を各 entry に載せる（相対パスの絶対化に必要）。

    omp-pi の編集記録はパスの大半が相対（edit 323/343・write 475/568）で、
    絶対化しないと不変条件3で全部落ちる。cwd を持つ session エントリはファイル先頭付近
    （実測では常に2行目）にあり、増分インデックス時には None プレースホルダに
    置き換えられて raw_entries から消えるため、その場合はファイルを直接読み直す。
    """
    cwd = None
    for entry in raw_entries:
        if isinstance(entry, dict) and entry.get("type") == "session":
            candidate = entry.get("cwd")
            if isinstance(candidate, str) and candidate:
                cwd = candidate
                break
    if cwd is None:
        cwd = _read_omp_pi_session_cwd(jsonl_path)
    if cwd is None:
        return

    for entry in raw_entries:
        if isinstance(entry, dict):
            entry["cwd"] = cwd


def _read_omp_pi_session_cwd(jsonl_path: Path, max_lines: int = 20) -> str | None:
    """ファイル先頭から session エントリの cwd を読む（増分インデックス時の退避経路）。"""
    if not jsonl_path.exists():
        return None
    with jsonl_path.open(encoding="utf-8") as f:
        for index, line in enumerate(f):
            if index >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "session":
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    return None


def parse_opencode_exchanges(
    source_path: str,
    raw_entries: list[dict | None],
    min_chars: int = 50,
) -> list[Exchange]:
    """OpenCode の message/part envelope 列をユーザーメッセージ単位の exchange に分割する。

    raw_entries は _load_opencode_raw_entries が (time_created, id) 順に並べた
    message/part envelope。message envelope のうち role="user" が exchange の境界になる。
    """
    conversation_id = sha256(source_path)
    message_role: dict[str, str] = {
        entry["id"]: entry["data"].get("role")
        for entry in raw_entries
        if isinstance(entry, dict) and entry.get("kind") == "message"
    }
    boundaries = [
        index
        for index, entry in enumerate(raw_entries)
        if isinstance(entry, dict)
        and entry.get("kind") == "message"
        and entry["data"].get("role") == "user"
    ]
    assistant_ids = {
        msg_id for msg_id, role in message_role.items() if role == "assistant"
    }

    exchanges: list[Exchange] = []
    for boundary_index, start in enumerate(boundaries):
        end = (
            boundaries[boundary_index + 1] - 1
            if boundary_index + 1 < len(boundaries)
            else len(raw_entries) - 1
        )
        user_entry = raw_entries[start]
        if user_entry is None:
            continue
        user_message_id = user_entry["id"]
        user_text = _opencode_message_text(raw_entries, start, end, {user_message_id})
        agent_text = _opencode_message_text(raw_entries, start, end, assistant_ids)
        if len(user_text + agent_text) < min_chars:
            continue

        touch_slice = raw_entries[start : end + 1]
        touches = opencode_adapter.extract_code_touches(touch_slice)
        files = list(dict.fromkeys(touch.file_path for touch in touches))
        exchanges.append(
            Exchange(
                id=sha256(f"{conversation_id}:{user_message_id}"),
                conversation_id=conversation_id,
                ply_start=start,
                ply_end=end,
                user_content=user_text,
                agent_content=agent_text,
                files=files,
                # opencode の project/session テーブルには worktree/directory/vcs は
                # あるが git ブランチ列は無い (tests/fixtures/harness_logs/README.md の
                # 実 opencode.db 再調査で確認済み。message/part の data JSON にも
                # ブランチ相当のキーは登場しない)。実在しないフィールドを捏造せず
                # None のままにする（issue #19）。
                git_branch=None,
            )
        )

    return exchanges


def _opencode_message_text(
    raw_entries: list[dict | None],
    start: int,
    end: int,
    message_ids: set[str],
) -> str:
    """[start, end] 範囲内で指定 message_id に属す text part の本文を連結する。"""
    return "\n".join(
        entry["data"]["text"]
        for entry in raw_entries[start : end + 1]
        if isinstance(entry, dict)
        and entry.get("kind") == "part"
        and entry.get("message_id") in message_ids
        and isinstance(entry.get("data"), dict)
        and entry["data"].get("type") == "text"
        and isinstance(entry["data"].get("text"), str)
    )


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat()


def _parse_opencode_row(
    kind: str, row: sqlite3.Row, session_id: str
) -> tuple[int, str, dict] | None:
    """message/part の1行を envelope に変換する。

    不正 JSON・NULL の time_created/data、および dict でない JSON 値
    （null・配列・文字列など）を持つ破損行は None を返す
    （1行の破損が DB 全体の取り込みを中断させないよう、呼び出し側でスキップする）。
    """
    try:
        data = json.loads(row["data"])
        if not isinstance(data, dict):
            return None
        envelope: dict = {
            "kind": kind,
            "id": row["id"],
            "session_id": session_id,
            "timestamp": _epoch_ms_to_iso(row["time_created"]),
            "data": data,
        }
        if kind == "part":
            envelope["message_id"] = row["message_id"]
        return row["time_created"], row["id"], envelope
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return None


def _load_opencode_raw_entries(
    src: sqlite3.Connection, session_id: str
) -> tuple[list[dict | None], str]:
    """OpenCode session DB から message/part を (time_created, id) 順の envelope 列にする。

    id は opencode 側で時刻順に単調生成される保証がないため、time_created を
    第一キーにし、id を tie-break にする。started_at には最初の envelope の時刻を使う
    （DB ファイルの mtime は全セッションで同一になり使えないため）。
    """
    messages = src.execute(
        "SELECT id, time_created, data FROM message WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    parts = src.execute(
        "SELECT id, message_id, time_created, data FROM part WHERE session_id = ?",
        (session_id,),
    ).fetchall()

    ordered: list[tuple[int, str, dict]] = []
    for row in messages:
        parsed = _parse_opencode_row("message", row, session_id)
        if parsed is not None:
            ordered.append(parsed)
    for row in parts:
        parsed = _parse_opencode_row("part", row, session_id)
        if parsed is not None:
            ordered.append(parsed)
    ordered.sort(key=lambda item: (item[0], item[1]))

    raw_entries: list[dict | None] = [entry for _, _, entry in ordered]
    started_at = (
        _epoch_ms_to_iso(ordered[0][0]) if ordered else datetime.now(UTC).isoformat()
    )
    return raw_entries, started_at


def _is_legacy_opencode_turn_id(source_turn_id: str) -> bool:
    """このパッチより前の index_opencode_db が書き込んだ position ベースの
    source_turn_id（str(ply_start)、短い数値文字列）かどうかを判定する。

    新スキームの source_turn_id は常に sha256 hexdigest（64桁の16進文字列）で、
    数字だけになる確率は無視できるほど低いため、「全桁が数字」かつ「64桁未満」を
    旧スキームの判定に使う。
    """
    return source_turn_id.isdigit() and len(source_turn_id) < 64


def index_opencode_db(
    opencode_db_path: Path,
    db_path: Path,
    min_chars: int = 50,
    project_root: Path | None = None,
) -> int:
    """OpenCode の SQLite セッション DB から project_root に属すセッションだけを取り込む。

    OpenCode の1 DB には全プロジェクトのセッションが混在するため、project.worktree が
    project_root の実体パスと一致するセッションのみを対象にする
    （openspec/changes/add-harness-adapters: 「OpenCode は worktree を信頼」）。
    DB は使用中の可能性があるため読み取り専用で開く。
    Returns: 新規登録した exchange 数（全セッション合計）
    """
    from codeatrium.db import get_connection

    if project_root is None:
        return 0

    db_uri = f"file:{quote(str(opencode_db_path), safe='/')}?mode=ro"
    src = sqlite3.connect(db_uri, uri=True)
    src.row_factory = sqlite3.Row
    try:
        project_root_real = os.path.realpath(str(project_root))
        project_ids = [
            row["id"]
            for row in src.execute("SELECT id, worktree FROM project")
            if row["worktree"] is not None
            and os.path.realpath(row["worktree"]) == project_root_real
        ]
        if not project_ids:
            return 0

        placeholders = ",".join("?" * len(project_ids))
        session_rows = src.execute(
            f"SELECT id FROM session WHERE project_id IN ({placeholders})",
            project_ids,
        ).fetchall()

        con = get_connection(db_path)
        total = 0
        try:
            for session_row in session_rows:
                session_id = session_row["id"]
                source_path = f"{opencode_db_path}#{session_id}"

                # ply_start はセッション内での位置添字で、新規メッセージが既存より
                # 古い time_created で到着すると添字が全体シフトする。位置ではなく
                # exchange.id（user message id 由来で位置非依存）で既取り込み分を
                # 判定し、旧ターンの再emit/新規ターンの取りこぼしを防ぐ。
                #
                # 後方互換: このパッチより前に取り込んだ行は source_turn_id に
                # str(ply_start)（位置そのもの）を格納しており、位置は安定した
                # identity ではない。アップグレード後に新規メッセージが
                # out-of-order で到着すると位置が全体シフトするため、position での
                # 突き合わせでは新規メッセージを誤って「既知」扱いし、旧 exchange を
                # 新ハッシュ id で二重登録してしまう（#48 レビュー指摘）。
                # 位置ではなく実際の内容（user_content・agent_content の組）で
                # 旧行と現在のパース結果を突き合わせ、一致した旧行の
                # source_turn_id / ply_start / ply_end / session_ref を新しい
                # 位置・スキームへその場で書き換える。id/canonical_exchange_id は
                # 変更しない — code_touches / exchange_files / palace_objects /
                # vec_exchanges からの exchange_id 参照はそのまま有効であり、以後の
                # 重複判定は本関数の事前フィルタのみで行われるため書き換え不要。
                #
                # 同一セッション内で (user_content, agent_content) が完全一致する
                # legacy exchange が複数存在し得る（同じ発話の繰り返し等）ため、
                # content キーごとに候補を list で保持し、元の ply_start 昇順で
                # FIFO 消費する。old メッセージ同士の相対順序は新規メッセージの
                # 挿入位置に関わらず保存される（time_created が不変なため）ので、
                # 現在のパース結果を ply_start 昇順で辿る順序と一致し、1対1で
                # 正しく対応付けられる。単一 dict スロットだと2件目が1件目を
                # 上書きし、シフト後に片方が永久に重複したまま残ってしまう。
                existing_rows = con.execute(
                    "SELECT source_turn_id, ply_start, user_content, agent_content "
                    "FROM exchanges WHERE harness = 'opencode' "
                    "AND source_session_id = ?",
                    (session_id,),
                ).fetchall()
                known_exchange_ids = {row["source_turn_id"] for row in existing_rows}
                legacy_by_content: dict[tuple[str, str], list[sqlite3.Row]] = {}
                for row in existing_rows:
                    if _is_legacy_opencode_turn_id(row["source_turn_id"]):
                        key = (row["user_content"], row["agent_content"])
                        legacy_by_content.setdefault(key, []).append(row)
                for candidates in legacy_by_content.values():
                    candidates.sort(key=lambda row: row["ply_start"])

                raw_entries, started_at = _load_opencode_raw_entries(src, session_id)
                exchanges = parse_opencode_exchanges(
                    source_path, raw_entries, min_chars=min_chars
                )
                new_exchanges = []
                for exchange in exchanges:
                    if exchange.id in known_exchange_ids:
                        continue
                    candidates = legacy_by_content.get(
                        (exchange.user_content, exchange.agent_content)
                    )
                    if candidates:
                        legacy_row = candidates.pop(0)
                        con.execute(
                            "UPDATE exchanges SET source_turn_id = ?, "
                            "ply_start = ?, ply_end = ?, session_ref = ? "
                            "WHERE harness = 'opencode' AND source_session_id = ? "
                            "AND source_turn_id = ?",
                            (
                                exchange.id,
                                exchange.ply_start,
                                exchange.ply_end,
                                f"{source_path}#ply="
                                f"{exchange.ply_start}-{exchange.ply_end}",
                                session_id,
                                legacy_row["source_turn_id"],
                            ),
                        )
                        continue
                    new_exchanges.append(exchange)
                if not new_exchanges:
                    continue

                from codeatrium.core.ingest import ingest_parse_result
                from codeatrium.core.models import (
                    CanonicalExchange,
                    CanonicalSession,
                    ExchangeArtifacts,
                    FileRename,
                    ParseResult,
                )

                artifacts = []
                extract_renames = getattr(
                    opencode_adapter, "extract_file_renames", None
                )
                for exchange in new_exchanges:
                    entry_slice = raw_entries[
                        exchange.ply_start : exchange.ply_end + 1
                    ]
                    renames = (
                        tuple(
                            FileRename(old_path, new_path, ts)
                            for old_path, new_path, ts in extract_renames(
                                entry_slice
                            )
                        )
                        if extract_renames is not None
                        else ()
                    )
                    touches = tuple(
                        opencode_adapter.extract_code_touches(entry_slice)
                    )
                    if touches or renames:
                        artifacts.append(
                            ExchangeArtifacts(
                                source_turn_id=exchange.id,
                                code_touches=touches,
                                file_renames=renames,
                            )
                        )
                result = ParseResult(
                    exchanges=tuple(
                        CanonicalExchange(
                            harness="opencode",
                            session_ref=(
                                f"{source_path}#ply="
                                f"{exchange.ply_start}-{exchange.ply_end}"
                            ),
                            source_session_id=session_id,
                            source_turn_id=exchange.id,
                            ply_start=exchange.ply_start,
                            ply_end=exchange.ply_end,
                            user_content=exchange.user_content,
                            agent_content=exchange.agent_content,
                            files_touched=tuple(exchange.files),
                            git_branch=exchange.git_branch,
                        )
                        for exchange in new_exchanges
                    ),
                    next_cursor=f"v1:ply:{new_exchanges[-1].ply_end}",
                    artifacts=tuple(artifacts),
                )
                total += ingest_parse_result(
                    con,
                    CanonicalSession(
                        harness="opencode",
                        source_session_id=session_id,
                        primary_ref=source_path,
                        project_key=str(project_root),
                        started_at=started_at,
                    ),
                    result,
                )
            con.commit()
        finally:
            con.close()
        return total
    finally:
        src.close()


def index_file(
    jsonl_path: Path,
    db_path: Path,
    min_chars: int = 50,
    project_root: Path | None = None,
    harness: str = "claude",
) -> int:
    """
    .jsonl ファイルを DB に登録する。
    既存 conversation の場合は last_ply_end 以降の新規 exchange のみ追加する。
    project_root を渡すと、あわせて code_touches（design §4.1）を同じコミットで記録する。
    None の場合は code_touches の記録をスキップする（project_root が無いと相対パス化できないため）。
    harness はログ形式と編集記録の抽出器を選ぶ。claude / codex / omp-pi / grok に対応する
    （opencode は SQLite なので index_opencode_db が別経路になる）。
    Returns: 新規登録した exchange 数
    """
    from codeatrium.db import get_connection

    if harness == "claude":
        parse = parse_exchanges
        touch_adapter = claude_adapter
    elif harness == "codex":
        parse = parse_codex_exchanges
        touch_adapter = codex_adapter
    elif harness == "omp-pi":
        parse = parse_omp_pi_exchanges
        touch_adapter = omp_pi_adapter
    elif harness == "grok":
        parse = parse_grok_exchanges
        touch_adapter = grok_adapter
    else:
        raise ValueError(f"Unsupported harness: {harness}")

    conversation_id = sha256(str(jsonl_path))
    con = get_connection(db_path)

    # 既存 conversation の last_ply_end を取得
    row = con.execute(
        "SELECT last_ply_end FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    last_ply_end = row["last_ply_end"] if row is not None else -1

    raw_entries = _load_raw_entries(jsonl_path, last_ply_end)
    if harness == "omp-pi":
        # 編集記録の抽出より前に cwd を載せる（相対パスの絶対化に必要）
        _annotate_omp_pi_cwd(jsonl_path, raw_entries)
    exchanges = parse(
        jsonl_path,
        min_chars=min_chars,
        last_ply_end=last_ply_end,
        raw_entries=raw_entries,
    )
    new_exchanges = [ex for ex in exchanges if ex.ply_start > last_ply_end]

    if not new_exchanges:
        con.close()
        return 0

    from codeatrium.core.ingest import ingest_parse_result
    from codeatrium.core.models import (
        CanonicalExchange,
        CanonicalSession,
        ExchangeArtifacts,
        FileRename,
        ParseResult,
    )

    source_session_id = str(jsonl_path.resolve())
    session = CanonicalSession(
        harness=harness,
        source_session_id=source_session_id,
        primary_ref=str(jsonl_path),
        project_key=str(project_root) if project_root is not None else "",
        started_at=datetime.fromtimestamp(
            jsonl_path.stat().st_mtime, tz=UTC
        ).isoformat(),
    )
    artifacts = []
    extract_renames = getattr(touch_adapter, "extract_file_renames", None)
    for exchange in new_exchanges:
        entry_slice = raw_entries[exchange.ply_start : exchange.ply_end + 1]
        renames = (
            tuple(
                FileRename(old_path, new_path, ts)
                for old_path, new_path, ts in extract_renames(entry_slice)
            )
            if extract_renames is not None
            else ()
        )
        touches = tuple(touch_adapter.extract_code_touches(entry_slice))
        if touches or renames:
            artifacts.append(
                ExchangeArtifacts(
                    source_turn_id=str(exchange.ply_start),
                    code_touches=touches,
                    file_renames=renames,
                )
            )
    result = ParseResult(
        exchanges=tuple(
            CanonicalExchange(
                harness=harness,
                session_ref=f"{jsonl_path}#ply={exchange.ply_start}-{exchange.ply_end}",
                source_session_id=source_session_id,
                source_turn_id=str(exchange.ply_start),
                ply_start=exchange.ply_start,
                ply_end=exchange.ply_end,
                user_content=exchange.user_content,
                agent_content=exchange.agent_content,
                files_touched=tuple(exchange.files),
                git_branch=exchange.git_branch,
            )
            for exchange in new_exchanges
        ),
        next_cursor=f"v1:ply:{new_exchanges[-1].ply_end}",
        artifacts=tuple(artifacts),
    )
    count = ingest_parse_result(con, session, result)
    con.commit()
    con.close()
    return count


