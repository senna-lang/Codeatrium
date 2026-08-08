"""
コードとの紐付けの前処理を担うハーネス非依存の純関数群（design §4.1・§5.3）。

ここに置く関数はファイル・DB に触れない。絶対パスをプロジェクト内相対パスへ
正規化する `normalize_repo_path` は、不変条件3（プロジェクト外は記録しない）
の判定そのものであり、記録前に必ず通す。`build_code_touch_rows` は CodeTouch を
code_touches テーブルの行データへ変換する。
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Any

from codeatrium.models import CodeTouch, LineRange, TextAnchor
from codeatrium.utils import sha256

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
