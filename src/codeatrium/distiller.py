"""蒸留モジュール: claude -p で exchange を palace object に変換する

SPEC Section 6 DISTILLER フロー準拠:
  ① files_touched を regex で抽出（LLM非使用）
  ② claude -p で palace object 生成（--output-format json --json-schema）
  ③ bm25_text を組み立てて palace_fts に登録
  ④ distill_text を embedding して vec_palace に登録
  ⑤ files_touched を tree-sitter で解析してシンボルを symbols テーブルに登録
"""

from __future__ import annotations

import datetime
import hashlib
import re
import struct
from pathlib import Path
from typing import Any

from codeatrium.embedder import Embedder
from codeatrium.llm import DISTILL_PROMPT_TEMPLATE, call_claude
from codeatrium.models import PalaceObject

# ---- ファイルパス抽出 ----

_FILES_PATTERN = re.compile(
    r"(/(?:[a-zA-Z0-9._\-]+/)*[a-zA-Z0-9._\-]+\.[a-zA-Z0-9]+)"  # 絶対パス
    r"|([a-zA-Z0-9._\-]+(?:/[a-zA-Z0-9._\-]+)+\.[a-zA-Z0-9]+)"  # 相対パス（1段以上のディレクトリ）
)


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
        path = m[0] or m[1]
        if path and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def distill_exchange(
    exchange_id: str,
    user_content: str,
    agent_content: str,
    ply_start: int,
    ply_end: int,
    model: str | None = None,
) -> PalaceObject:
    """1つの exchange を蒸留して PalaceObject を返す"""
    messages_text = (user_content + "\n" + agent_content)[:4000]
    prompt = DISTILL_PROMPT_TEMPLATE.format(
        ply_start=ply_start,
        ply_end=ply_end,
        messages_text=messages_text,
    )
    raw = call_claude(prompt, model=model)
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
    embedding: Any,  # np.ndarray
) -> None:
    """PalaceObject を DB に保存し exchange の distilled_at を更新する"""
    import numpy as np

    from codeatrium.db import get_connection

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
    exists = con.execute(
        "SELECT 1 FROM vec_palace WHERE palace_id = ?", (palace_id,)
    ).fetchone()
    if not exists:
        con.execute(
            "INSERT INTO vec_palace (palace_id, embedding) VALUES (?, ?)",
            (palace_id, blob),
        )

    # ⑤ tree-sitter シンボル解決
    from codeatrium.resolver import SymbolResolver

    resolver = SymbolResolver()
    for file_str in palace.files_touched:
        for sym in resolver.extract(Path(file_str)):
            sym_id = _sha256(f"{sym.symbol_name}:{sym.file_path}")
            con.execute(
                """
                INSERT OR IGNORE INTO symbols
                    (id, palace_object_id, symbol_name, symbol_kind,
                     file_path, signature, line, dedup_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sym_id,
                    palace_id,
                    sym.symbol_name,
                    sym.symbol_kind,
                    sym.file_path,
                    sym.signature,
                    sym.line,
                    sym_id,
                ),
            )

    con.execute(
        "UPDATE exchanges SET distilled_at = ? WHERE id = ?",
        (datetime.datetime.utcnow().isoformat(), exchange_id),
    )

    con.commit()
    con.close()


def distill_all(
    db_path: Path, limit: int | None = None, model: str | None = None
) -> int:
    """未蒸留の exchange を処理する。Returns: 処理した exchange 数"""
    from codeatrium.db import get_connection

    con = get_connection(db_path)
    query = """
        SELECT e.id, e.user_content, e.agent_content, e.ply_start, e.ply_end
        FROM exchanges e
        WHERE e.distilled_at IS NULL
          AND (SELECT COUNT(*) FROM exchanges e2
               WHERE e2.conversation_id = e.conversation_id) >= 2
    """
    if limit is not None:
        query += f" LIMIT {limit}"
    rows = con.execute(query).fetchall()
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
            model=model,
        )
        distill_text = palace.exchange_core + "\n" + palace.specific_context
        vec = embedder.embed_passage(distill_text)
        save_palace_object(db_path, row["id"], palace, vec)
        count += 1

    return count
