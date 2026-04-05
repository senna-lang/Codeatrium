"""蒸留モジュール: claude -p で exchange を palace object に変換する

SPEC Section 6 DISTILLER フロー準拠:
  ① files_touched を regex で抽出（LLM非使用）
  ② claude -p で palace object 生成（--output-format json --json-schema）
  ③ distill_text を embedding して vec_palace に登録
  ④ files_touched を tree-sitter で解析してシンボルを symbols テーブルに登録
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

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


_EXTERNAL_PATH_MARKERS = (
    "site-packages/",
    "dist-packages/",
    "/lib/python",
    "/opt/",
    "/usr/lib/",
    "/usr/local/lib/",
    ".venv/",
    "/venv/",
    "node_modules/",
)


def _is_external_path(path: str, project_root: str | None = None) -> bool:
    """プロジェクト外のパスか判定する。

    絶対パス: project_root が指定されていればその配下かチェック。
    相対パス: ハードコードマーカーでフィルタ。
    """
    if path.startswith("/"):
        # 絶対パス: project_root 配下でなければ外部
        if project_root:
            return not path.startswith(project_root)
        # project_root 不明時はマーカーでフォールバック
    return any(marker in path for marker in _EXTERNAL_PATH_MARKERS)


def extract_files_touched(
    user_content: str, agent_content: str, project_root: str | None = None
) -> list[str]:
    """user_content + agent_content から regex でファイルパスを抽出する（重複排除・順序維持）

    project_root が指定された場合、絶対パスはその配下のもののみ残す。
    相対パスはハードコードマーカー（node_modules 等）でフィルタする。
    """
    text = user_content + "\n" + agent_content
    # project_root を末尾スラッシュ付きに正規化
    root_prefix = (project_root.rstrip("/") + "/") if project_root else None
    seen: set[str] = set()
    result: list[str] = []
    for m in _FILES_PATTERN.findall(text):
        path = m[0] or m[1]
        if path and path not in seen and not _is_external_path(path, root_prefix):
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
    project_root: str | None = None,
) -> PalaceObject:
    """1つの exchange を蒸留して PalaceObject を返す"""
    messages_text = (user_content + "\n" + agent_content)[:4000]
    prompt = DISTILL_PROMPT_TEMPLATE.format(
        ply_start=ply_start,
        ply_end=ply_end,
        messages_text=messages_text,
    )
    raw = call_claude(prompt, model=model)
    files_touched = extract_files_touched(
        user_content, agent_content, project_root=project_root
    )
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

    con = get_connection(db_path)

    con.execute(
        """
        INSERT OR IGNORE INTO palace_objects
            (id, exchange_id, exchange_core, specific_context, distill_text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            palace_id,
            exchange_id,
            palace.exchange_core,
            palace.specific_context,
            distill_text,
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
    db_path: Path,
    limit: int | None = None,
    model: str | None = None,
    on_progress: Callable[..., None] | None = None,
    project_root: str | None = None,
    distill_min_chars: int = 100,
) -> int:
    """未蒸留の exchange を処理する。

    distill_min_chars: この文字数未満の exchange は蒸留スキップ（デフォルト100）
    on_progress: (current, total, error=None) を受け取るコールバック
    Returns: 処理した exchange 数
    """
    from codeatrium.db import get_connection

    con = get_connection(db_path)

    # 蒸留対象外の exchange を skipped にマーク:
    # - 1-exchange セッション
    # - distill_min_chars 未満（ワンフレーズ指示・システムメッセージ等）
    con.execute("""
        UPDATE exchanges SET distilled_at = 'skipped'
        WHERE distilled_at IS NULL
          AND ((SELECT COUNT(*) FROM exchanges e2
                WHERE e2.conversation_id = exchanges.conversation_id) < 2
               OR LENGTH(user_content) + LENGTH(agent_content) < ?)
    """, (distill_min_chars,))
    con.commit()

    query = """
        SELECT e.id, e.user_content, e.agent_content, e.ply_start, e.ply_end
        FROM exchanges e
        WHERE e.distilled_at IS NULL
    """
    if limit is not None:
        query += f" LIMIT {limit}"
    rows = con.execute(query).fetchall()
    con.close()

    if not rows:
        return 0

    total = len(rows)
    embedder = Embedder()
    count = 0
    errors = 0
    for row in rows:
        try:
            palace = distill_exchange(
                row["id"],
                row["user_content"],
                row["agent_content"],
                row["ply_start"],
                row["ply_end"],
                model=model,
                project_root=project_root,
            )
            distill_text = palace.exchange_core + "\n" + palace.specific_context
            vec = embedder.embed_passage(distill_text)
            save_palace_object(db_path, row["id"], palace, vec)
            count += 1
        except Exception as e:
            errors += 1
            if on_progress is not None:
                on_progress(count, total, error=str(e))
            continue
        if on_progress is not None:
            on_progress(count, total)

    return count
