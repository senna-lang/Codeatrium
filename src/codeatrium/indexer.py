"""
.jsonl パース・exchange 分割・DB 保存

exchange 境界定義:
  role="user" かつ isMeta!=true かつ実質的なテキスト発話を持つエントリから
  次の同様エントリの直前まで。ツール呼び出し・中間応答は同一 exchange に含める。

フィルタルール（SPEC Section 6 / 論文 Section 3.1 準拠）:
  - 50文字未満の exchange は trivial として除外
  - isMeta=True の user エントリは exchange 境界としない

project_root を渡すと、exchange と同じコミットで code_touches（design §4.1）も記録する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeatrium.adapters.harness import claude as claude_adapter
from codeatrium.code_touches import (
    build_code_touch_rows,
    is_external_path,
    normalize_repo_path,
)
from codeatrium.utils import sha256


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
            if name not in {'Edit', 'Write', 'Read', 'MultiEdit', 'NotebookEdit'}:
                continue

            input_dict = block.get("input")
            if not isinstance(input_dict, dict):
                continue

            # NotebookEdit の場合は notebook_path、その他は file_path
            path = None
            if name == 'NotebookEdit':
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
    boundaries: list[int] = [i for i, e in enumerate(raw_entries) if e is not None and _is_real_user_entry(e)]

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
        git_branch = git_branch_raw if isinstance(git_branch_raw, str) and git_branch_raw.strip() else None
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


def index_file(
    jsonl_path: Path,
    db_path: Path,
    min_chars: int = 50,
    project_root: Path | None = None,
) -> int:
    """
    .jsonl ファイルを DB に登録する。
    既存 conversation の場合は last_ply_end 以降の新規 exchange のみ追加する。
    project_root を渡すと、あわせて code_touches（design §4.1）を同じコミットで記録する。
    None の場合は code_touches の記録をスキップする（project_root が無いと相対パス化できないため）。
    Returns: 新規登録した exchange 数
    """
    from codeatrium.db import get_connection

    conversation_id = sha256(str(jsonl_path))
    con = get_connection(db_path)

    # 既存 conversation の last_ply_end を取得
    row = con.execute(
        "SELECT last_ply_end FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    last_ply_end = row["last_ply_end"] if row is not None else -1

    raw_entries = _load_raw_entries(jsonl_path, last_ply_end)
    exchanges = parse_exchanges(
        jsonl_path, min_chars=min_chars, last_ply_end=last_ply_end, raw_entries=raw_entries
    )
    new_exchanges = [ex for ex in exchanges if ex.ply_start > last_ply_end]

    if not new_exchanges:
        con.close()
        return 0

    # conversations に登録 or 更新
    mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC).isoformat()
    if row is None:
        con.execute(
            "INSERT INTO conversations (id, source_path, started_at, last_ply_end) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, str(jsonl_path), mtime, new_exchanges[-1].ply_end),
        )
    else:
        con.execute(
            "UPDATE conversations SET last_ply_end = ? WHERE id = ?",
            (new_exchanges[-1].ply_end, conversation_id),
        )

    for ex in new_exchanges:
        con.execute(
            """
            INSERT OR IGNORE INTO exchanges
                (id, conversation_id, ply_start, ply_end, user_content, agent_content, git_branch)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ex.id,
                ex.conversation_id,
                ex.ply_start,
                ex.ply_end,
                ex.user_content,
                ex.agent_content,
                ex.git_branch,
            ),
        )

    # exchange_files を登録
    for ex in new_exchanges:
        for file_path in ex.files:
            con.execute(
                "INSERT OR IGNORE INTO exchange_files (exchange_id, file_path) VALUES (?, ?)",
                (ex.id, file_path),
            )

    # code_touches を登録（design §5.3: 解析 → まとめて INSERT → 既存コミットにまとめる）
    if project_root is not None:
        for ex in new_exchanges:
            exchange_slice = raw_entries[ex.ply_start : ex.ply_end + 1]
            for touch in claude_adapter.extract_code_touches(exchange_slice):
                rel_path = normalize_repo_path(touch.file_path, str(project_root))
                if rel_path is None:
                    continue
                for touch_row in build_code_touch_rows(touch, exchange_id=ex.id, rel_file_path=rel_path):
                    con.execute(
                        """
                        INSERT OR IGNORE INTO code_touches
                            (id, exchange_id, harness, tool_call_id, file_path, touch_kind,
                             locator_kind, old_start, old_lines, new_start, new_lines,
                             old_string, new_string, added, removed, ts)
                        VALUES (:id, :exchange_id, :harness, :tool_call_id, :file_path, :touch_kind,
                                :locator_kind, :old_start, :old_lines, :new_start, :new_lines,
                                :old_string, :new_string, :added, :removed, :ts)
                        """,
                        touch_row,
                    )

    con.commit()
    con.close()
    return len(new_exchanges)
