"""
コードとの紐付けを担うハーネス非依存の純関数群（design §2.4・§4.1・§5.3・§5.5）。

ここに置く関数はファイル・DB・ディスクに触れない。絶対パスをプロジェクト内相対パスへ
正規化する `normalize_repo_path` は、不変条件3（プロジェクト外は記録しない）の判定
そのものであり、記録前に必ず通す。`build_code_touch_rows` は CodeTouch を code_touches
テーブルの行データへ変換する。`resolve_line_range` / `resolve_symbol_name` は編集記録
から「どの関数を触ったか」をディスクを読まずに best-effort で推定する（design §2.4）。
特定できなければ None を返す — 間違ったひも付けは、ひも付けが無いことより悪い（§3.3）。

`intersect_span` / `touches_to_edges` は別の経路で、tree-sitter が解決した
シンボルの行範囲（`resolver.Symbol`、呼び出し側がディスクを読んで用意する）と
編集された行範囲の重なりで CodeEdge を作る（design §5.5）。§8.1 の不変条件
（編集記録1件からは必ず1本以上、シンボル不明でもファイル粒度で必ず1本）を守る。
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from codeatrium.models import CodeEdge, CodeLocator, CodeTouch, LineRange, TextAnchor
from codeatrium.utils import sha256

if TYPE_CHECKING:
    # 型注釈だけの利用。tree-sitter の重い読み込みをこのモジュールの実行時コストにしない
    from codeatrium.resolver import Symbol

# granularity ごとの確信度（design §6.3 の GRAN 重みをそのまま流用）。
# line: シンボルの行範囲との重なりが確認できた「確信」。file: シンボル不明の下限。
LINE_CONFIDENCE = 1.0
FILE_CONFIDENCE = 0.5

# 外部パス（サイトパッケージ・依存ディレクトリ）の判定用マーカー。
# indexer.py の tool_use file 抽出と同じ基準を共有する。
EXTERNAL_PATH_MARKERS = (
    'site-packages/',
    'dist-packages/',
    '/lib/python',
    '/opt/',
    '/usr/lib/',
    '/usr/local/lib/',
    '.venv/',
    '/venv/',
    'node_modules/',
)


def is_external_path(path: str) -> bool:
    """パスが外部ライブラリ（site-packages など）を指しているか判定する"""
    return any(marker in path for marker in EXTERNAL_PATH_MARKERS)


def normalize_repo_path(file_path: str, project_root: str) -> str | None:
    """絶対パスをプロジェクトルートからの相対パスへ正規化する（design §5.3）。

    プロジェクト外・外部ライブラリ・相対パス入力は None を返す（不変条件3）。
    文字列の前方一致では隣接リポジトリ（例: repo と repo-other）を誤って内部と
    判定してしまうため、パス部品ごとに比較する（不具合G）。
    """
    if not file_path.startswith("/"):
        return None

    file_parts = PurePosixPath(os.path.normpath(file_path)).parts
    root_parts = PurePosixPath(os.path.normpath(project_root)).parts

    if file_parts[: len(root_parts)] != root_parts:
        return None

    rel_parts = file_parts[len(root_parts):]
    if not rel_parts:
        return None

    rel_path = "/".join(rel_parts)
    if is_external_path(rel_path):
        return None

    return rel_path


def build_code_touch_rows(
    touch: CodeTouch, exchange_id: str, rel_file_path: str
) -> list[dict[str, Any]]:
    """CodeTouch を code_touches テーブルの行（1件以上）に変換する純関数（design §4.1）。

    LineRange が複数あれば hunk ごとに1行作る（`seq` で id を分ける）。
    TextAnchor は各行にそのまま複製して残す（生データを捨てない — principle②）。
    LineRange が無ければ TextAnchor、それも無ければ FileOnly の1行に落ちる。
    """
    line_ranges = [loc for loc in touch.locators if isinstance(loc, LineRange)]
    anchor = next((loc for loc in touch.locators if isinstance(loc, TextAnchor)), None)

    def _row(seq: int, locator_kind: str, line_range: LineRange | None) -> dict[str, Any]:
        row_id = sha256(f"{exchange_id}:{touch.tool_call_id}:{rel_file_path}:{seq}")
        return {
            "id": row_id,
            "exchange_id": exchange_id,
            "harness": touch.harness,
            "tool_call_id": touch.tool_call_id,
            "file_path": rel_file_path,
            "touch_kind": touch.touch_kind,
            "locator_kind": locator_kind,
            "old_start": line_range.old_start if line_range else None,
            "old_lines": line_range.old_lines if line_range else None,
            "new_start": line_range.new_start if line_range else None,
            "new_lines": line_range.new_lines if line_range else None,
            "old_string": anchor.old_string if anchor else None,
            "new_string": anchor.new_string if anchor else None,
            "added": touch.added,
            "removed": touch.removed,
            "ts": touch.ts,
        }

    if line_ranges:
        return [_row(seq, "line", lr) for seq, lr in enumerate(line_ranges)]
    if anchor is not None:
        return [_row(0, "anchor", None)]
    return [_row(0, "file", None)]


def resolve_line_range(
    locators: tuple[CodeLocator, ...], original_content: str | None
) -> tuple[int, int] | None:
    """手がかりを行の範囲（開始行, 終了行）へ変換する（design §5.3、1-indexed 両端含む）。

    優先順位は LineRange → TextAnchor。ここで返す行範囲は常に旧ファイル側の座標になる
    （LineRange の old_start、または TextAnchor を original_content 中から探した位置）。
    resolve_symbol_name が original_content（旧ファイル全文）と突き合わせるための座標が
    これで、新ファイル側の座標（new_start）とは別の目的である（§2.4「old_start の位置を解決する」）。

    行番号が欠けている LineRange は使えないので次の手がかりへフォールバックする。
    TextAnchor は old_string が原文中に無い・複数箇所にマッチする場合は推測せず None を返す（§3.3）。
    """
    for loc in locators:
        if isinstance(loc, LineRange) and loc.old_start is not None:
            old_lines = loc.old_lines if loc.old_lines and loc.old_lines > 0 else 1
            return (loc.old_start, loc.old_start + old_lines - 1)

    for loc in locators:
        if isinstance(loc, TextAnchor) and loc.old_string and original_content:
            start = _find_unique_line_span(loc.old_string, original_content)
            if start is not None:
                return start

    return None


def _find_unique_line_span(needle: str, haystack: str) -> tuple[int, int] | None:
    """haystack 中に needle がちょうど1箇所だけ現れる場合、その行範囲を返す（1-indexed）"""
    count = haystack.count(needle)
    if count != 1:
        return None
    offset = haystack.index(needle)
    start_line = haystack.count("\n", 0, offset) + 1
    # needle 末尾の改行は「次の行の開始」ではなく「この行の終端」なので行数に数えない
    span_lines = len(needle.splitlines()) or 1
    return (start_line, start_line + span_lines - 1)


# 言語ごとの関数・クラス定義行パターン（design §2.4: def / class / function / const X = ( ）
# resolver.py の tree-sitter とは独立した、テキストベースの best-effort ヒューリスティック。
_SYMBOL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    ".py": (
        re.compile(r"^\s*(?:async\s+)?def\s+(\w+)"),
        re.compile(r"^\s*class\s+(\w+)"),
    ),
    ".ts": (
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"),
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)"),
        re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*="),
    ),
    ".go": (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"),),
}
_SYMBOL_PATTERNS[".tsx"] = _SYMBOL_PATTERNS[".ts"]


def _match_symbol_line(line: str, lang: str) -> str | None:
    patterns = _SYMBOL_PATTERNS.get(lang)
    if not patterns:
        return None
    # diff の +/-/space プレフィックスを1文字だけ剥がす（patch_body 由来の行のため）
    stripped = line[1:] if line and line[0] in "+- " else line
    for pattern in patterns:
        m = pattern.match(stripped)
        if m:
            return m.group(1)
    return None


def _leading_whitespace_len(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _find_enclosing_declaration(lines: list[str], start_idx: int, lang: str) -> str | None:
    """start_idx（0-indexed）から手前へ、インデントに基づき最も内側の定義行を探す。

    単純に「手前で最初に見つかった def/class」を返すと、対象行が実はその定義の
    外（モジュールレベルの兄弟コードなど）にあっても誤って包含していると
    判定してしまう（自信満々で間違える最悪の失敗 — design §2.4・§3.3）。
    インデントが基準より浅い行だけを「外側の境界」として扱い、それが定義行で
    なければ基準を更新してさらに外側へ探索を続ける。
    """
    patterns = _SYMBOL_PATTERNS.get(lang)
    if not patterns:
        return None

    ref_idx = start_idx
    while ref_idx >= 0 and not lines[ref_idx].strip():
        ref_idx -= 1
    if ref_idx < 0:
        return None
    ref_indent = _leading_whitespace_len(lines[ref_idx])

    # 対象行自身が定義行ならそれを直接返す（シグネチャそのものを編集したケース）
    name = _match_symbol_line(lines[ref_idx], lang)
    if name is not None:
        return name

    idx = ref_idx - 1
    while idx >= 0:
        line = lines[idx]
        if not line.strip():
            idx -= 1
            continue
        indent = _leading_whitespace_len(line)
        if indent < ref_indent:
            name = _match_symbol_line(line, lang)
            if name is not None:
                return name
            ref_indent = indent  # 定義行ではない外側の構文（if/for 等）。基準を更新してさらに外へ
        idx -= 1
    return None


def resolve_symbol_name(
    line_range: tuple[int, int] | None,
    patch_body: list[str],
    original_content: str | None,
    lang: str,
) -> tuple[str | None, str | None]:
    """編集箇所を囲む関数・クラス名を best-effort で特定する（design §2.4・§5.3）。

    戻り値は (symbol_name, resolved_by)。ディスクは読まない。
    1. line_range と original_content（旧ファイル全文）が両方あれば、line_range の開始行
       から手前へ向かって最初に見つかった定義行を使う → 'original_file'
    2. 差分本文（patch_body）に定義行が含まれていればそこから読む → 'patch_body'
    3. どちらも駄目なら (None, None)。symbol_id=NULL でファイル粒度に落ちる（不変条件2）
    """
    if line_range is not None and original_content:
        lines = original_content.split("\n")
        start_line = line_range[0]
        if 1 <= start_line <= len(lines):
            name = _find_enclosing_declaration(lines, start_line - 1, lang)
            if name is not None:
                return (name, "original_file")

    # 追加行（+）を最優先で見る。リネームのような "-def old / +def new" では
    # 現在の名前（+側）を残したいため。次に文脈行、最後に削除行（-）の順で探す。
    added = [ln for ln in patch_body if ln.startswith("+")]
    removed = [ln for ln in patch_body if ln.startswith("-")]
    context = [ln for ln in patch_body if not ln.startswith("+") and not ln.startswith("-")]
    for bucket in (added, context, removed):
        for line in bucket:
            name = _match_symbol_line(line, lang)
            if name is not None:
                return (name, "patch_body")

    return (None, None)


def intersect_span(touch: CodeTouch, symbols: list[Symbol]) -> list[Symbol]:
    """編集された行の範囲と重なるシンボルを返す（design §5.5、純関数）。

    `touch.locators` に含まれる **全ての** `LineRange`（複数 hunk 分）を見る。
    1つの hunk が複数シンボルにまたがることはある（重なりを全部返す）が、
    同じシンボルが複数 hunk にまたがってヒットしても1回だけ返す。
    比較は新ファイル側の座標（`new_start`/`new_lines`）で行う——旧ファイル座標を
    使う `resolve_line_range` とは目的が別（design §2.4 実装ノート参照）。
    行範囲の手がかりが無い touch（anchor/file のみ）には常に空を返す。
    """
    matched: dict[str, Symbol] = {}
    for loc in touch.locators:
        if not isinstance(loc, LineRange):
            continue
        touch_lines = loc.new_lines if loc.new_lines and loc.new_lines > 0 else 1
        touch_start = loc.new_start
        touch_end = loc.new_start + touch_lines - 1
        for sym in symbols:
            if touch_start <= sym.end_line and sym.line <= touch_end:
                matched[sym.symbol_name] = sym
    return list(matched.values())


def touches_to_edges(
    touch: CodeTouch,
    exchange_id: str,
    rel_file_path: str,
    symbols: list[Symbol],
) -> list[CodeEdge]:
    """CodeTouch を code_edges の行へ変換する（design §5.5、純関数）。

    design §8.1 の不変条件を守る実装:
      - 不変条件1: touch 1件からは必ず1本以上の CodeEdge を返す
      - 不変条件2: シンボルが特定できなくても granularity='file' で必ず1本張る
        （「シンボルが見つからなければ何も作らない」は誤り。ここを間違えると
        G1 が静かに崩れる）

    `intersect_span` で重なりが見つかれば、シンボルごとに granularity='line' の
    確信度1.0のエッジを作る。見つからなければ（未対応言語・行範囲なし・重なりなし
    のいずれでも）granularity='file' のエッジを1本だけ作る。
    """
    matched = intersect_span(touch, symbols)

    if matched:
        edges = []
        for sym in matched:
            symbol_id = sha256(f"{rel_file_path}:{sym.symbol_name}")
            edge_id = sha256(
                f"{exchange_id}:{rel_file_path}:{symbol_id}:{touch.touch_kind}"
            )
            edges.append(
                CodeEdge(
                    id=edge_id,
                    exchange_id=exchange_id,
                    file_path=rel_file_path,
                    symbol_id=symbol_id,
                    edge_kind=touch.touch_kind,
                    granularity="line",
                    confidence=LINE_CONFIDENCE,
                    added=touch.added,
                    ts=touch.ts,
                )
            )
        return edges

    edge_id = sha256(f"{exchange_id}:{rel_file_path}::{touch.touch_kind}")
    return [
        CodeEdge(
            id=edge_id,
            exchange_id=exchange_id,
            file_path=rel_file_path,
            symbol_id=None,
            edge_kind=touch.touch_kind,
            granularity="file",
            confidence=FILE_CONFIDENCE,
            added=touch.added,
            ts=touch.ts,
        )
    ]
