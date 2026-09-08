"""蒸留モジュール: claude -p で exchange を palace object に変換する

SPEC Section 6 DISTILLER フロー準拠:
  ① files_touched を regex で抽出（LLM非使用）
  ② claude -p で palace object 生成（--output-format json --json-schema）
  ③ distill_text を embedding して vec_palace に登録
  ④ files_touched を tree-sitter で解析してシンボルを symbols テーブルに登録

backend パラメータで蒸留に使う DistillBackend を指定可能。
"""

from __future__ import annotations

import datetime
import os
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from codeatrium.code_touches import is_external_path, normalize_repo_path
from codeatrium.embedder import Embedder, EmbedderSetupError
from codeatrium.llm import DISTILL_PROMPT_TEMPLATE, DistillBackend, call_claude
from codeatrium.models import PalaceObject
from codeatrium.utils import sha256

if TYPE_CHECKING:
    from codeatrium.resolver import SymbolResolver

# ---- ファイルパス抽出 ----

_FILES_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?:"
    r"(/(?:[a-zA-Z0-9._\-]+/)*[a-zA-Z0-9._\-]+\.[a-zA-Z][a-zA-Z0-9]*)"  # 絶対パス
    r"|"
    r"((?:\.?[a-zA-Z0-9_\-]+)(?:/(?:\.?[a-zA-Z0-9_\-]+))*/[a-zA-Z0-9._\-]+\.[a-zA-Z][a-zA-Z0-9]*)"  # 相対パス（隠しディレクトリの先頭ドットのみ許容・1段以上、拡張子は英字始まり）
    r")"
)


# ---- 公開 API ----


def _is_external_path(path: str, project_root: str | None = None) -> bool:
    """プロジェクト外のパスか判定する。

    絶対パス: project_root が指定されていれば code_touches.normalize_repo_path の
    パス部品比較で判定する（文字列前方一致では隣接リポジトリ、例えば /home/u/repo と
    /home/u/repo-other を誤って同一プロジェクトと判定してしまうため、これを避ける）。
    相対パス、または project_root 不明時はハードコードマーカーでフォールバックする。
    """
    if path.startswith("/"):
        if project_root:
            return normalize_repo_path(path, project_root) is None
        # project_root 不明時はマーカーでフォールバック
    return is_external_path(path)


def extract_files_touched(
    user_content: str, agent_content: str, project_root: str | None = None
) -> list[str]:
    """user_content + agent_content から regex でファイルパスを抽出する（重複排除・順序維持）

    project_root が指定された場合、絶対パスはその配下のもののみ残す。
    相対パスはハードコードマーカー（node_modules 等）でフィルタする。
    """
    text = user_content + "\n" + agent_content
    seen: set[str] = set()
    result: list[str] = []
    for m in _FILES_PATTERN.findall(text):
        path = m[0] or m[1]
        if path and path not in seen and not _is_external_path(path, project_root):
            seen.add(path)
            result.append(path)
    return result


def distill_exchange(
    exchange_id: str,
    db_path: Path,
    user_content: str,
    agent_content: str,
    ply_start: int,
    ply_end: int,
    backend: DistillBackend | None = None,
    project_root: str | None = None,
) -> PalaceObject:
    """1つの exchange を蒸留して PalaceObject を返す

    Parameters:
        exchange_id: exchange の ID
        db_path: データベースファイルパス
        user_content: ユーザーコンテンツ
        agent_content: エージェントコンテンツ
        ply_start: 開始ply
        ply_end: 終了ply
        backend: 蒸留に使う DistillBackend（省略時はデフォルト claude）
        project_root: プロジェクトルート（ファイルパスフィルタ用）
    """
    from codeatrium.db import get_connection

    messages_text = (user_content + "\n" + agent_content)[:4000]
    prompt = DISTILL_PROMPT_TEMPLATE.format(
        ply_start=ply_start,
        ply_end=ply_end,
        messages_text=messages_text,
    )
    raw = call_claude(prompt, backend=backend)

    # PRIMARY: exchange_files から読み込み
    con = get_connection(db_path)
    rows = con.execute(
        "SELECT file_path FROM exchange_files WHERE exchange_id=?", (exchange_id,)
    ).fetchall()
    con.close()
    primary_paths = [row["file_path"] for row in rows]

    # FALLBACK: regex 抽出
    fallback_paths = extract_files_touched(
        user_content, agent_content, project_root=project_root
    )

    # Merge: primary パスをフィルタして、fallback との重複排除
    seen: set[str] = set()
    files_touched: list[str] = []

    for path in primary_paths:
        if path not in seen and not _is_external_path(path, project_root):
            seen.add(path)
            files_touched.append(path)

    for path in fallback_paths:
        if path not in seen:
            seen.add(path)
            files_touched.append(path)

    return PalaceObject(
        exchange_core=raw["exchange_core"],
        specific_context=raw["specific_context"],
        room_assignments=raw["room_assignments"],
        files_touched=files_touched,
    )


# ECMAScript IdentifierPart は Unicode ID_Continue、`$`、ZWNJ、ZWJ から成る。
# ID_Continue は ID_Start（Other_ID_Start を含む）、追加 category 群、Other_ID_Continue を
# 合わせた導出プロパティである。正規表現依存を増やさず stdlib の Unicode database から
# category を判定し、category だけでは表せない例外を明示する。
_UNICODE_ID_CONTINUE_CATEGORIES = frozenset(
    {"Lu", "Ll", "Lt", "Lm", "Lo", "Nl", "Mn", "Mc", "Nd", "Pc"}
)

_OTHER_ID_START = frozenset(
    {
        "\u1885",
        "\u1886",
        "\u2118",
        "\u212e",
        "\u309b",
        "\u309c",
    }
)
_OTHER_ID_CONTINUE = frozenset(
    {
        "\u00b7",
        "\u0387",
        "\u1369",
        "\u136a",
        "\u136b",
        "\u136c",
        "\u136d",
        "\u136e",
        "\u136f",
        "\u1370",
        "\u1371",
        "\u19da",
    }
)
_ECMASCRIPT_IDENTIFIER_PART_EXTRAS = frozenset({"$", "\u200c", "\u200d"})


def _is_identifier_part(character: str) -> bool:
    return (
        character in _ECMASCRIPT_IDENTIFIER_PART_EXTRAS
        or unicodedata.category(character) in _UNICODE_ID_CONTINUE_CATEGORIES
        or character in _OTHER_ID_START
        or character in _OTHER_ID_CONTINUE
    )


def _decode_unicode_escape_before(text: str, index: int) -> str | None:
    """index の直前で終わる ECMAScript Unicode escape を1文字だけ復元する。"""
    fixed_width_start = index - 6
    if fixed_width_start >= 0 and text.startswith(r"\u", fixed_width_start):
        digits = text[fixed_width_start + 2 : index]
        if all(character in "0123456789abcdefABCDEF" for character in digits):
            return chr(int(digits, 16))

    if index >= 5 and text[index - 1] == "}":
        for digit_count in range(1, 7):
            escape_start = index - digit_count - 4
            if escape_start < 0 or not text.startswith(r"\u{", escape_start):
                continue
            digits = text[escape_start + 3 : index - 1]
            if all(character in "0123456789abcdefABCDEF" for character in digits):
                code_point = int(digits, 16)
                if code_point <= 0x10FFFF:
                    return chr(code_point)

    return None


def _is_identifier_part_before(text: str, index: int) -> bool:
    """text の index 直前が ECMAScript IdentifierPart か判定する。"""
    if index == 0:
        return False
    escaped_character = _decode_unicode_escape_before(text, index)
    if escaped_character is not None:
        return _is_identifier_part(escaped_character)
    return _is_identifier_part(text[index - 1])


def _symbol_mentioned_in_body(symbol_name: str, body_text: str) -> bool:
    """symbol_name が body_text 中に識別子境界つきで出現するか判定する。"""
    start = body_text.find(symbol_name)
    while start != -1:
        end = start + len(symbol_name)
        previous_is_identifier_part = _is_identifier_part_before(body_text, start)
        next_is_identifier_part = end < len(body_text) and _is_identifier_part(
            body_text[end]
        )
        if not previous_is_identifier_part and not next_is_identifier_part:
            return True
        start = body_text.find(symbol_name, start + 1)
    return False


def save_palace_object(
    db_path: Path,
    exchange_id: str,
    palace: PalaceObject,
    embedding: Any,  # np.ndarray
    resolver: SymbolResolver | None = None,
    symbol_cache: dict[str, list[Any]] | None = None,
    project_root: str | None = None,
) -> None:
    """PalaceObject を DB に保存し exchange の distilled_at を更新する

    project_root が指定された場合、files_touched の相対パスはプロセスの CWD ではなく
    project_root を基準に解決してから tree-sitter でシンボル抽出する。
    """
    import numpy as np

    from codeatrium.db import get_connection
    from codeatrium.resolver import SymbolResolver

    palace_id = sha256(f"palace:{exchange_id}")
    distill_text = palace.exchange_core + "\n" + palace.specific_context

    con = get_connection(db_path)
    con.execute("BEGIN")
    try:
        existing = con.execute(
            "SELECT 1 FROM palace_objects WHERE id = ?", (palace_id,)
        ).fetchone()
        if existing is None:
            con.execute(
                """
                INSERT INTO palace_objects
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
        verify = con.execute(
            "SELECT 1 FROM palace_objects WHERE id = ?", (palace_id,)
        ).fetchone()
        if verify is None:
            raise RuntimeError(f"palace_objects INSERT failed for id={palace_id}")

        for room in palace.room_assignments:
            dedup = sha256(f"{room['room_type']}:{room['room_key']}")
            room_id = sha256(f"{palace_id}:{dedup}")
            room_exists = con.execute(
                "SELECT 1 FROM rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room_exists is None:
                con.execute(
                    """
                    INSERT INTO rooms
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

        blob = embedding.astype(np.float32).tobytes()
        exists = con.execute(
            "SELECT 1 FROM vec_palace WHERE palace_id = ?", (palace_id,)
        ).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO vec_palace (palace_id, embedding) VALUES (?, ?)",
                (palace_id, blob),
            )

        # ⑤ tree-sitter シンボル解決。旧 `symbols` の palace ごとの重複ではなく、
        # code_symbols（正本）と code_edges（exchange との関係）へ保存する。
        if resolver is None:
            from codeatrium.resolver import SymbolResolver

            resolver = SymbolResolver()

        # Fetch exchange body text for symbol body-mention filter
        ex_row = con.execute(
            "SELECT user_content, agent_content FROM exchanges WHERE id = ?",
            (exchange_id,),
        ).fetchone()
        body_text = (
            (ex_row["user_content"] + ex_row["agent_content"])
            if ex_row is not None
            else ""
        )

        for file_str in palace.files_touched:
            if symbol_cache is not None and file_str in symbol_cache:
                syms = symbol_cache[file_str]
            else:
                resolved_path = Path(file_str)
                if project_root and not resolved_path.is_absolute():
                    resolved_path = Path(project_root) / resolved_path
                syms = resolver.extract(resolved_path)
                if symbol_cache is not None:
                    symbol_cache[file_str] = syms
            for sym in syms:
                # Body-mention filter: skip symbol if not mentioned in exchange
                # （単語境界つき。部分一致だと1文字シンボル名が全マッチしてしまう）
                if not _symbol_mentioned_in_body(sym.symbol_name, body_text):
                    continue

                file_path = sym.file_path
                if project_root:
                    normalized = normalize_repo_path(file_path, project_root)
                    if normalized is None:
                        continue
                    file_path = normalized
                symbol_id = sha256(f"{file_path}:{sym.symbol_name}")
                con.execute(
                    """
                    INSERT OR IGNORE INTO code_symbols
                        (id, file_path, symbol_name, symbol_kind, signature,
                         line, end_line, lang, resolved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol_id,
                        file_path,
                        sym.symbol_name,
                        sym.symbol_kind,
                        sym.signature,
                        sym.line,
                        sym.end_line,
                        sym.lang,
                        datetime.datetime.now(datetime.UTC).isoformat(),
                    ),
                )
                edge_id = sha256(f"{exchange_id}:{file_path}:{symbol_id}:distill")
                con.execute(
                    """
                    INSERT OR IGNORE INTO code_edges
                        (id, exchange_id, file_path, symbol_id, edge_kind,
                         granularity, confidence, added, ts)
                    VALUES (?, ?, ?, ?, 'distill', 'line', 1.0, 0, NULL)
                    """,
                    (edge_id, exchange_id, file_path, symbol_id),
                )

        con.execute(
            "UPDATE exchanges SET distilled_at = ?, distill_status = 'distilled' WHERE id = ?",
            (datetime.datetime.now(datetime.UTC).isoformat(), exchange_id),
        )

        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def distill_all(
    db_path: Path,
    limit: int | None = None,
    backend: DistillBackend | None = None,
    on_progress: Callable[..., None] | None = None,
    project_root: str | None = None,
    distill_min_chars: int = 100,
) -> tuple[int, int]:
    """未蒸留の exchange を処理する。

    backend: 蒸留に使う DistillBackend（省略時はデフォルト claude）
    distill_min_chars: この文字数未満の exchange は蒸留スキップ（デフォルト100）
    on_progress: (current, total, error=None) を受け取るコールバック
    Returns: (処理した exchange 数, エラー数)
    """
    from codeatrium.db import get_connection
    from codeatrium.resolver import SymbolResolver

    con = get_connection(db_path)

    # 蒸留対象外の exchange を skipped にマーク:
    # - 1-exchange セッション
    # - distill_min_chars 未満（ワンフレーズ指示・システムメッセージ等）
    con.execute(
        """
        UPDATE exchanges SET distilled_at = 'skipped', distill_status = 'skipped'
        WHERE distilled_at IS NULL
          AND ((SELECT COUNT(*) FROM exchanges e2
                WHERE e2.conversation_id = exchanges.conversation_id) < 2
               OR LENGTH(user_content) + LENGTH(agent_content) < ?)
    """,
        (distill_min_chars,),
    )
    con.commit()

    query = """
        SELECT e.id, e.user_content, e.agent_content, e.ply_start, e.ply_end
        FROM exchanges e
        WHERE e.distill_status = 'pending'
    """
    params: list[int] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    rows = con.execute(query, params).fetchall()
    con.close()

    if not rows:
        return 0, 0

    total = len(rows)
    embedder = Embedder()
    resolver = SymbolResolver()
    symbol_cache: dict[str, list[Any]] = {}
    count = 0
    errors = 0
    for row in rows:
        try:
            palace = distill_exchange(
                row["id"],
                db_path,
                row["user_content"],
                row["agent_content"],
                row["ply_start"],
                row["ply_end"],
                backend=backend,
                project_root=project_root,
            )
            distill_text = palace.exchange_core + "\n" + palace.specific_context
            vec = embedder.embed_passage(distill_text)
            save_palace_object(
                db_path,
                row["id"],
                palace,
                vec,
                resolver=resolver,
                symbol_cache=symbol_cache,
                project_root=project_root,
            )
            count += 1
        except EmbedderSetupError:
            # 環境レベルの失敗: per-row でなくループ全体を中断する
            raise
        except Exception as e:
            errors += 1
            if on_progress is not None:
                on_progress(count, total, error=str(e))
            continue
        if on_progress is not None:
            on_progress(count, total)

    return count, errors
