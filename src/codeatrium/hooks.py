"""Claude Code hook 設定の JSON 操作ロジック。

lifecycle イベント→コマンドの対応は `adapters.harness.lifecycle.lifecycle_commands()`
が唯一の正規情報源（design doc harness-hook-abstraction.md §4.1）。ここでは
settings.json への差分検出・自動修復・matcher マージという Claude 固有の
idempotency ロジックだけを担う。

堅牢性についての設計上の制約（issue #28）:
- SessionStart の既存 loci フック検知は matcher 文字列に依存しない（ユーザーが
  matcher をカスタマイズしていても正準 matcher に限定せず全エントリを横断する）。
- loci が発行したコマンドかどうかは `"loci"` という語の部分一致ではなく、
  コマンドをトークン分割した上で「`<venv>/bin/loci` という構造を持つ絶対パス」
  であるかを判定する（無関係なユーザーコマンドの誤検知/誤削除を防ぎつつ、
  install 時と別の virtualenv からインストールされた hook でも
  `loci_bin()` の絶対パス一致に依存せず uninstall で安全に認識できる）。
- settings.json の JSON パース失敗は `SettingsLoadError` に変換し、生の
  トレースバックではなく actionable なメッセージを返した上で書き込みを拒否する。
- `.bak` は上書き前にタイムスタンプ付きアーカイブへ退避し、直近 N 世代を保持する
  （2回連続の不正な書き込みで唯一の backup が失われる問題への対策）。
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import tempfile
from datetime import datetime
from itertools import count
from pathlib import Path, PurePosixPath
from typing import Any, cast

from codeatrium.adapters.harness.lifecycle import lifecycle_commands
from codeatrium.config import DEFAULT_DISTILL_BATCH_LIMIT

_CANONICAL_MATCHER = "startup|clear|resume|compact"
_MANAGED_ACTIONS: tuple[str, ...] = ("index", "server", "distill", "prime")
_MAX_BACKUP_GENERATIONS = 5

_backup_counter = count()


class SettingsLoadError(RuntimeError):
    """settings.json の JSON パースに失敗した場合に送出する。

    生の `json.JSONDecodeError` トレースバックをそのまま CLI に露出させると
    非actionable な exit になる（issue #28）ため、書き込みを拒否した上で
    修復方法を示すメッセージへ変換する。
    """


def _load_settings(settings_path: Path) -> dict[str, Any]:
    """settings.json を読み込む。壊れている場合は書き込みを拒否する。"""
    with settings_path.open(encoding="utf-8") as f:
        try:
            return cast(dict[str, Any], json.load(f))
        except json.JSONDecodeError as exc:
            raise SettingsLoadError(
                f"{settings_path} contains invalid JSON ({exc}). "
                "Fix or remove the file before retrying — refusing to write to "
                "avoid corrupting it further."
            ) from exc


def _is_loci_binary_token(token: str) -> bool:
    """token がこの codeatrium がインストールした loci バイナリ
    （`<venv>/bin/loci`）を指しているかどうかを判定する。

    `loci_bin()` の絶対パスと厳密一致させると、install 時と異なる
    virtualenv から `loci hook uninstall` を実行した場合に既存の hook を
    一切認識できず、削除されずに残ってしまう（PR #54 レビュー指摘）。
    そのため実行時パスには依存せず、トークンをパスとして解釈したときの
    「basename が `loci` かつ親ディレクトリ名が `bin`」という構造だけで
    判定する。単に語として "loci" を含むコマンド（例: `my-loci-backup.sh`）
    は basename が一致しないため誤検知しない。
    """
    path = PurePosixPath(token)
    return path.is_absolute() and path.name == "loci" and path.parent.name == "bin"


def _command_owned_by_loci(
    cmd: str, actions: tuple[str, ...] = _MANAGED_ACTIONS
) -> bool:
    """cmd が（どの virtualenv からインストールされたかを問わず）loci が
    発行した managed hook コマンドかどうかを判定する。

    旧実装は `"loci" in cmd` という部分一致で判定していたため、パスに "loci"
    を含むだけの無関係なユーザーコマンド（例: `~/tools/my-loci-backup.sh
    --index`）を誤検知し、install 時の誤上書きや uninstall 時の誤削除を
    招いていた（issue #28）。コマンドを shlex でトークン分割し、
    `_is_loci_binary_token` で構造的に loci バイナリだと判定できるトークンが
    存在する場合のみ「自分が発行したコマンド」と判定する。
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    return any(_is_loci_binary_token(t) for t in tokens) and any(
        action in tokens for action in actions
    )


def _find_hook_by_action(
    session_start_hooks: list[dict[str, Any]], action: str
) -> dict[str, Any] | None:
    """SessionStart の全エントリを matcher を問わず横断して、指定 action の
    既存 loci コマンドを持つ hook dict を探す。

    正準 matcher (`_CANONICAL_MATCHER`) に限定して検索すると、ユーザーが
    matcher をカスタマイズしている場合に検知漏れとなり index/server/distill/
    prime コマンドが重複登録される（issue #28）。
    """
    for entry in session_start_hooks:
        for h in entry.get("hooks", []):
            if _command_owned_by_loci(h.get("command", ""), (action,)):
                return h
    return None


def _rotate_backup(bak_path: Path) -> None:
    """既存の `.bak` をタイムスタンプ付きアーカイブへ退避し、直近 N 世代を保持する。

    旧実装は毎回 `.json.bak` を無条件に上書きしていたため、2回連続で不正な
    書き込みが起きると直前の正常な backup も失われていた（issue #28）。
    """
    suffix = f"{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.{next(_backup_counter):06d}"
    archive_path = bak_path.with_name(f"{bak_path.name}.{suffix}")
    shutil.move(str(bak_path), str(archive_path))

    archives = sorted(bak_path.parent.glob(f"{bak_path.name}.*"))
    for stale in archives[:-_MAX_BACKUP_GENERATIONS]:
        stale.unlink(missing_ok=True)


def _write_settings(settings_path: Path, settings: dict[str, Any]) -> None:
    """settings.json をアトミックに書き込む。書き込み前に .bak を作成し os.replace で差し替える。"""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        bak_path = settings_path.with_suffix(".json.bak")
        if bak_path.exists():
            _rotate_backup(bak_path)
        shutil.copy2(settings_path, bak_path)

    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        dir=settings_path.parent,
        delete=False,
        suffix=".tmp",
        encoding="utf-8",
    )
    tmp_path = tmp_file.name
    try:
        json.dump(settings, tmp_file, ensure_ascii=False, indent=2)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.replace(tmp_path, settings_path)
    except Exception:
        tmp_file.close()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _install_stop_hook(stop_hooks: list[dict[str, Any]], index_cmd: str) -> bool:
    """Stop フック（loci index, async: true）を検知・自動修復する。"""
    changed = False
    installed = False
    for entry in stop_hooks:
        for h in entry.get("hooks", []):
            if _command_owned_by_loci(h.get("command", ""), ("index",)):
                installed = True
                if h.get("command") != index_cmd or not h.get("async"):
                    h["command"] = index_cmd
                    h["async"] = True
                    h.pop("nohup", None)
                    changed = True
    if not installed:
        stop_hooks.append(
            {"hooks": [{"type": "command", "command": index_cmd, "async": True}]}
        )
        changed = True
    return changed


def _install_session_start_hooks(
    session_start_hooks: list[dict[str, Any]],
    server_cmd: str,
    distill_cmd: str,
    prime_cmd: str,
) -> bool:
    """SessionStart フック（server start / distill / prime）を検知・自動修復する。

    matcher の文字列一致では検知できない（ユーザーが matcher をカスタマイズして
    いると別物として再インストールされ重複登録される）ため、matcher を問わず
    全 SessionStart エントリを横断して既存 loci フックを探す。新規追加時のみ、
    正準 matcher のエントリへまとめる。
    """
    changed = False
    for action, cmd in (
        ("server", server_cmd),
        ("distill", distill_cmd),
        ("prime", prime_cmd),
    ):
        existing = _find_hook_by_action(session_start_hooks, action)
        if existing is not None:
            if existing.get("command") != cmd:
                existing["command"] = cmd
                changed = True
            continue
        target_entry = next(
            (e for e in session_start_hooks if e.get("matcher") == _CANONICAL_MATCHER),
            None,
        )
        if target_entry is None:
            target_entry = {"matcher": _CANONICAL_MATCHER, "hooks": []}
            session_start_hooks.append(target_entry)
        cast(list[dict[str, Any]], target_entry["hooks"]).append(
            {"type": "command", "command": cmd}
        )
        changed = True
    return changed


def _cleanup_legacy_session_end(hooks: dict[str, Any]) -> bool:
    """古い SessionEnd の loci distill エントリがあれば削除する。"""
    if "SessionEnd" not in hooks:
        return False
    before = hooks["SessionEnd"]
    after = [
        entry
        for entry in before
        if not any(
            _command_owned_by_loci(h.get("command", ""), ("distill",))
            for h in entry.get("hooks", [])
        )
    ]
    if after:
        hooks["SessionEnd"] = after
    else:
        del hooks["SessionEnd"]
    return after != before


def install_hooks(batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> tuple[bool, str]:
    """Claude Code の Stop / SessionStart フックに loci を登録する。

    Returns: (changed, message) — 変更の有無と結果メッセージ
    """
    settings_path = Path.home() / ".claude" / "settings.json"

    if settings_path.exists():
        settings: dict[str, Any] = _load_settings(settings_path)
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    commands = lifecycle_commands("claude", batch_limit)
    index_cmd = commands.on_turn_end
    server_cmd, distill_cmd, prime_cmd = commands.on_session_start

    stop_hooks: list[dict[str, Any]] = hooks.setdefault("Stop", [])
    session_start_hooks: list[dict[str, Any]] = hooks.setdefault("SessionStart", [])
    changed = _install_stop_hook(stop_hooks, index_cmd)
    changed |= _install_session_start_hooks(
        session_start_hooks, server_cmd, distill_cmd, prime_cmd
    )
    changed |= _cleanup_legacy_session_end(hooks)

    if not changed:
        return False, "Hooks already up to date."

    _write_settings(settings_path, settings)

    lines = [
        f"Hooks installed: {settings_path}",
        f"  Stop (async):       {index_cmd}",
        f"  SessionStart:       {server_cmd}",
        f"  SessionStart:       {distill_cmd}",
        f"  SessionStart:       {prime_cmd}",
        f"  (matcher: {_CANONICAL_MATCHER})",
    ]
    return True, "\n".join(lines)


def uninstall_hooks() -> tuple[bool, str]:
    """codeatrium に関連する Claude Code フックを削除する。

    Returns: (changed, message) — 変更の有無と結果メッセージ
    """
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        return False, "No settings.json found. Nothing to uninstall."

    settings: dict[str, Any] = _load_settings(settings_path)
    settings_before = copy.deepcopy(settings)

    hooks = settings.get("hooks", {})
    if not hooks:
        return False, "No hooks section found. Nothing to uninstall."

    for section in ("Stop", "SessionStart", "SessionEnd"):
        if section not in hooks:
            continue
        entries: list[dict[str, Any]] = hooks[section]
        for entry in entries[:]:
            hooks_list = entry.get("hooks", [])
            entry["hooks"] = [
                h
                for h in hooks_list
                if not _command_owned_by_loci(h.get("command", ""))
            ]
        hooks[section] = [e for e in entries if e.get("hooks")]
        if not hooks[section]:
            del hooks[section]

    if not hooks:
        if "hooks" in settings:
            del settings["hooks"]

    if settings == settings_before:
        return False, "No codeatrium hooks found. Nothing to uninstall."

    _write_settings(settings_path, settings)
    return True, f"Hooks uninstalled: {settings_path}"
