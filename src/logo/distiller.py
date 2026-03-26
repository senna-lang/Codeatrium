"""
蒸留モジュール: claude -p で exchange を palace object に変換する

SPEC Section 6 DISTILLER フロー準拠:
  ① files_touched を regex で抽出（LLM非使用）
  ② claude -p で palace object 生成（--output-format json --json-schema）
  ③ bm25_text を組み立てて palace_fts に登録
  ④ distill_text を embedding して vec_palace に登録
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from logo.embedder import Embedder

# ---- ファイルパス抽出 ----

_FILES_PATTERN = re.compile(
    r"(/(?:[a-zA-Z0-9._\-]+/)*[a-zA-Z0-9._\-]+\.[a-zA-Z0-9]+)"  # 絶対パス
    r"|([a-zA-Z0-9._\-]+(?:/[a-zA-Z0-9._\-]+)+\.[a-zA-Z0-9]+)"  # 相対パス（1段以上のディレクトリ）
)

# ---- プロンプト定数 ----

_DISTILL_PROMPT_TEMPLATE = """\
この対話のやり取りをJSONに蒸留してください：

- "exchange_core": 1-2文。何が達成または決定されましたか？\
やり取り内の特定の用語を使用してください。\
テキストに存在しない詳細を捏造しないでください。\
やり取りがほぼ空の場合は、簡潔にその旨を述べてください。
- "specific_context": テキストからの具体的な詳細1つ：\
数値、エラーメッセージ、パラメータ名、またはファイルパス。\
テキストから正確にコピーしてください。プロジェクトパスは使用しないでください。
- "room_assignments": 1-3個の部屋。各部屋はこのやり取りが属するトピックです。\
{{"room_type": "<file|concept|workflow>", "room_key": "<識別子>",\
 "room_label": "<短いラベル>", "relevance": <0.0-1.0>}}\
部屋は関連するやり取りをグループ化するのに十分具体的なものにしてください\
（例：「errors」ではなく「retry_timeout」）。

"files_touched"は含めないでください。

やり取り (メッセージ {ply_start}-{ply_end}): {messages_text}

JSONのみで回答してください。"""

_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "exchange_core": {"type": "string", "maxLength": 300},
            "specific_context": {"type": "string", "maxLength": 200},
            "room_assignments": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "room_type": {
                            "type": "string",
                            "enum": ["file", "concept", "workflow"],
                        },
                        "room_key": {"type": "string"},
                        "room_label": {"type": "string"},
                        "relevance": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["room_type", "room_key", "room_label", "relevance"],
                },
            },
        },
        "required": ["exchange_core", "specific_context", "room_assignments"],
    }
)

# ---- データクラス ----


@dataclass
class PalaceObject:
    """蒸留済み palace object"""

    exchange_core: str
    specific_context: str
    room_assignments: list[dict[str, Any]]
    files_touched: list[str] = field(default_factory=list)


# ---- 内部ヘルパー ----


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---- 公開 API ----


def extract_files_touched(user_content: str, agent_content: str) -> list[str]:
    """user_content + agent_content から regex でファイルパスを抽出する（重複排除・順序維持）"""
    text = user_content + "\n" + agent_content
    seen: set[str] = set()
    result: list[str] = []
    for m in _FILES_PATTERN.findall(text):
        path = m[0] or m[1]  # 絶対パス or 相対パス
        if path and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def call_claude(prompt: str) -> dict[str, Any]:
    """claude -p でプロンプトを実行し JSON を返す（テストでモック対象）"""
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model",
            "claude-haiku-4-5-20251001",
            "--output-format",
            "json",
            "--json-schema",
            _JSON_SCHEMA,
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr}")
    return json.loads(result.stdout)


def distill_exchange(
    exchange_id: str,
    user_content: str,
    agent_content: str,
    ply_start: int,
    ply_end: int,
) -> PalaceObject:
    """1つの exchange を蒸留して PalaceObject を返す"""
    messages_text = (user_content + "\n" + agent_content)[:4000]
    prompt = _DISTILL_PROMPT_TEMPLATE.format(
        ply_start=ply_start,
        ply_end=ply_end,
        messages_text=messages_text,
    )
    raw = call_claude(prompt)
    files_touched = extract_files_touched(user_content, agent_content)
    return PalaceObject(
        exchange_core=raw["exchange_core"],
        specific_context=raw["specific_context"],
        room_assignments=raw["room_assignments"],
        files_touched=files_touched,
    )


def save_palace_object(
    db_path: Path,
    exchange_id: str,
    palace: PalaceObject,
    embedding: "Any",  # np.ndarray
) -> None:
    """PalaceObject を DB に保存し exchange の distilled_at を更新する"""
    import numpy as np

    from logo.db import get_connection

    palace_id = _sha256(f"palace:{exchange_id}")
    distill_text = palace.exchange_core + "\n" + palace.specific_context
    bm25_text = " ".join(
        [
            palace.exchange_core,
            palace.specific_context,
            " ".join(palace.files_touched),
            " ".join(r["room_key"] for r in palace.room_assignments),
            " ".join(r["room_label"] for r in palace.room_assignments),
        ]
    )

    con = get_connection(db_path)

    con.execute(
        """
        INSERT OR IGNORE INTO palace_objects
            (id, exchange_id, exchange_core, specific_context, distill_text, bm25_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            palace_id,
            exchange_id,
            palace.exchange_core,
            palace.specific_context,
            distill_text,
            bm25_text,
        ),
    )

    for room in palace.room_assignments:
        dedup = _sha256(f"{room['room_type']}:{room['room_key']}")
        room_id = _sha256(f"{palace_id}:{dedup}")
        con.execute(
            """
            INSERT OR IGNORE INTO rooms
                (id, palace_object_id, room_type, room_key, room_label, relevance, dedup_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                palace_id,
                room["room_type"],
                room["room_key"],
                room["room_label"],
                room["relevance"],
                dedup,
            ),
        )

    arr = embedding.astype(np.float32)
    blob = struct.pack(f"{len(arr)}f", *arr.tolist())
    con.execute(
        "INSERT OR IGNORE INTO vec_palace (palace_id, embedding) VALUES (?, ?)",
        (palace_id, blob),
    )

    con.execute(
        "UPDATE exchanges SET distilled_at = ? WHERE id = ?",
        (datetime.datetime.utcnow().isoformat(), exchange_id),
    )

    con.commit()
    con.close()


def distill_all(db_path: Path) -> int:
    """未蒸留の exchange を全て処理する。Returns: 処理した exchange 数"""
    from logo.db import get_connection

    con = get_connection(db_path)
    rows = con.execute(
        """
        SELECT id, user_content, agent_content, ply_start, ply_end
        FROM exchanges
        WHERE distilled_at IS NULL
        """
    ).fetchall()
    con.close()

    if not rows:
        return 0

    embedder = Embedder()
    count = 0
    for row in rows:
        palace = distill_exchange(
            row["id"],
            row["user_content"],
            row["agent_content"],
            row["ply_start"],
            row["ply_end"],
        )
        distill_text = palace.exchange_core + "\n" + palace.specific_context
        vec = embedder.embed_passage(distill_text)
        save_palace_object(db_path, row["id"], palace, vec)
        count += 1

    return count
