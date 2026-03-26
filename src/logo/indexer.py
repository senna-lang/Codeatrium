"""
.jsonl パース・exchange 分割・DB 保存

exchange 境界定義:
  role="user" かつ isMeta!=true かつ実質的なテキスト発話を持つエントリから
  次の同様エントリの直前まで。ツール呼び出し・中間応答は同一 exchange に含める。

フィルタルール（SPEC Section 6 / 論文 Section 3.1 準拠）:
  - 100文字未満の exchange は trivial として除外
  - isMeta=True の user エントリは exchange 境界としない
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Exchange:
    """exchange 単位の verbatim テキスト"""

    id: str
    conversation_id: str
    ply_start: int
    ply_end: int
    user_content: str
    agent_content: str


# ---- 内部ヘルパー ----


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
    return bool(text.strip())


# ---- 公開API ----


def parse_exchanges(jsonl_path: Path) -> list[Exchange]:
    """
    .jsonl ファイルを読んで exchange リストを返す。
    trivial（100文字未満）は除外する。
    """
    entries: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    conversation_id = _sha256(str(jsonl_path))

    # exchange の境界インデックスを収集
    boundaries: list[int] = [i for i, e in enumerate(entries) if _is_real_user_entry(e)]

    exchanges: list[Exchange] = []
    for b_idx, start in enumerate(boundaries):
        end = (
            boundaries[b_idx + 1] - 1
            if b_idx + 1 < len(boundaries)
            else len(entries) - 1
        )

        user_entry = entries[start]
        user_text = _extract_text(user_entry["message"]["content"])

        # assistant の発話を連結
        agent_parts: list[str] = []
        for e in entries[start + 1 : end + 1]:
            if e.get("type") == "assistant":
                msg = e.get("message", {})
                if isinstance(msg, dict):
                    text = _extract_text(msg.get("content", ""))
                    if text:
                        agent_parts.append(text)

        agent_text = "\n".join(agent_parts)
        combined = user_text + agent_text

        # trivial フィルタ
        if len(combined) < 100:
            continue

        user_uuid = user_entry.get("uuid", f"{start}")
        exchange_id = _sha256(f"{conversation_id}:{user_uuid}")

        exchanges.append(
            Exchange(
                id=exchange_id,
                conversation_id=conversation_id,
                ply_start=start,
                ply_end=end,
                user_content=user_text,
                agent_content=agent_text,
            )
        )

    return exchanges


def index_file(jsonl_path: Path, db_path: Path) -> int:
    """
    .jsonl ファイルを DB に登録する。
    すでに登録済みの場合はスキップ（重複排除）。
    Returns: 新規登録した exchange 数
    """
    from logo.db import get_connection

    conversation_id = _sha256(str(jsonl_path))
    con = get_connection(db_path)

    # 重複チェック
    row = con.execute(
        "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is not None:
        con.close()
        return 0

    # conversations に登録
    con.execute(
        "INSERT INTO conversations (id, source_path) VALUES (?, ?)",
        (conversation_id, str(jsonl_path)),
    )

    exchanges = parse_exchanges(jsonl_path)
    for ex in exchanges:
        con.execute(
            """
            INSERT OR IGNORE INTO exchanges
                (id, conversation_id, ply_start, ply_end, user_content, agent_content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ex.id,
                ex.conversation_id,
                ex.ply_start,
                ex.ply_end,
                ex.user_content,
                ex.agent_content,
            ),
        )

    con.commit()
    con.close()
    return len(exchanges)
