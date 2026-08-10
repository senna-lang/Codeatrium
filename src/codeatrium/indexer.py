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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codeatrium.adapters.harness import claude as claude_adapter
from codeatrium.adapters.harness import codex as codex_adapter
from codeatrium.code_touches import (
    build_code_touch_rows,
    is_external_path,
    normalize_repo_path,
    touches_to_edges,
)
from codeatrium.utils import sha256

if TYPE_CHECKING:
    from codeatrium.resolver import Symbol


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
    harness はログ形式と編集記録の抽出器を選ぶ。現在は claude と codex に対応する。
    Returns: 新規登録した exchange 数
    """
    from codeatrium.db import get_connection

    if harness == "claude":
        parse = parse_exchanges
        touch_adapter = claude_adapter
    elif harness == "codex":
        parse = parse_codex_exchanges
        touch_adapter = codex_adapter
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

    # code_touches / code_symbols / code_edges を登録
    # （design §5.3: 解析 → まとめて INSERT → 既存コミットにまとめる。
    #  §8.1 の不変条件（編集したファイルは100%）は蒸留を待たずに満たす必要があるため、
    #  ここ index 時点で code_edges まで作り切る — distill 待ちにすると、蒸留の
    #  batch_limit/distill_min_chars でスキップされた touch が永久に0件のままになる）
    if project_root is not None:
        from codeatrium.resolver import SymbolResolver

        resolver = SymbolResolver()
        symbol_cache: dict[str, list[Symbol]] = {}
        resolved_at = datetime.now(UTC).isoformat()

        for ex in new_exchanges:
            exchange_slice = raw_entries[ex.ply_start : ex.ply_end + 1]
            if harness == "codex":
                for old_path, new_path, timestamp in codex_adapter.extract_file_renames(
                    exchange_slice
                ):
                    old_rel_path = normalize_repo_path(old_path, str(project_root))
                    new_rel_path = normalize_repo_path(new_path, str(project_root))
                    if old_rel_path is None or new_rel_path is None:
                        continue
                    con.execute(
                        """
                        INSERT INTO file_renames (old_path, new_path, source, ts)
                        VALUES (?, ?, 'harness', ?)
                        ON CONFLICT(old_path, new_path) DO UPDATE SET
                            source = excluded.source,
                            ts = excluded.ts
                        """,
                        (old_rel_path, new_rel_path, timestamp),
                    )

            for touch in touch_adapter.extract_code_touches(exchange_slice):
                rel_path = normalize_repo_path(touch.file_path, str(project_root))
                if rel_path is None:
                    continue

                for touch_row in build_code_touch_rows(
                    touch, exchange_id=ex.id, rel_file_path=rel_path
                ):
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

                if rel_path not in symbol_cache:
                    symbol_cache[rel_path] = resolver.extract(Path(touch.file_path))
                symbols = symbol_cache[rel_path]

                for sym in symbols:
                    symbol_id = sha256(f"{rel_path}:{sym.symbol_name}")
                    # REPLACE は id が変わらないシンボルの line/end_line/signature を
                    # 最新の解析結果へ更新するために使う（code_symbols が「唯一の正しい
                    # 定義元」であるため、§4.1）。SQLite の REPLACE は delete→insert だが、
                    # code_edges.symbol_id に FK は無いので安全。将来 FK を張るなら、
                    # ON DELETE CASCADE と組み合わせないこと（このREPLACEでedgeが消える）
                    con.execute(
                        """
                        INSERT OR REPLACE INTO code_symbols
                            (id, file_path, symbol_name, symbol_kind, signature,
                             line, end_line, lang, resolved_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            symbol_id,
                            rel_path,
                            sym.symbol_name,
                            sym.symbol_kind,
                            sym.signature,
                            sym.line,
                            sym.end_line,
                            sym.lang,
                            resolved_at,
                        ),
                    )

                edges = touches_to_edges(
                    touch, exchange_id=ex.id, rel_file_path=rel_path, symbols=symbols
                )
                for edge in edges:
                    # id は (exchange_id, file_path, symbol_id, edge_kind) から決まるため、
                    # 同じ exchange 内で同じシンボルを別の touch（別 tool_call）が触ると
                    # 衝突する。INSERT OR IGNORE だと後から来た touch の added が
                    # 静かに失われる（§6.3 の log1p(added) が過小評価になる）ので、
                    # 衝突時は加算する。code_touches は last_ply_end で重複処理されない
                    # ため、この加算が再インデックスで二重計上されることもない。
                    con.execute(
                        """
                        INSERT INTO code_edges
                            (id, exchange_id, file_path, symbol_id, edge_kind,
                             granularity, confidence, added, ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET added = added + excluded.added
                        """,
                        (
                            edge.id,
                            edge.exchange_id,
                            edge.file_path,
                            edge.symbol_id,
                            edge.edge_kind,
                            edge.granularity,
                            edge.confidence,
                            edge.added,
                            edge.ts,
                        ),
                    )

    con.commit()
    con.close()
    return len(new_exchanges)
