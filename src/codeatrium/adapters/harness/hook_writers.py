"""harness の「ファイル所有モデル」ごとに共有する hook writer 実装（design doc §3, §4.2）。

分類軸はフォーマットではなく書き込み先の所有モデル:

- `MergedJsonHookWriter`: 共有 JSON 設定ファイルへコマンド単位でマージ/削除する。
  claude/codex と同じ `{"hooks": {Event: [{matcher?, hooks: [...]}]}}` 形式を
  codex, grok が共有する（claude 自身は `codeatrium.hooks` の専用実装のまま。
  差分自動修復や `async` フラグなど claude 固有の細かい idempotency 挙動を持ち、
  テストが `codeatrium.hooks.Path.home`/`loci_bin` を直接パッチしているため、
  この一般化には含めない）。
- `DedicatedFileWriter`: ディレクトリ自動検出される専用ファイル1本を生成する
  （omp-pi, opencode）。マーカーコメントで「codeatrium が書いたファイル」だけを
  対象にし、同じ共有ディレクトリ（`~/.omp/agent/extensions/`,
  `~/.config/opencode/plugins/`）に置かれた他ツール製ファイルを誤削除しない。
  これは issue #28（`"loci" in cmd` 部分一致 uninstall / 単一 .bak の頑健性問題）が
  指摘した「共有ディレクトリでの誤削除」への対策をここに一元実装したもの。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeatrium.adapters.harness.lifecycle import LifecycleCommands

# DedicatedFileWriter が生成する全ファイルに埋め込む所有マーカー。
# アンインストール時、このマーカーを含まないファイルは他ツール/ユーザー製と
# 判断し、一切触らない（マーカーが無ければ削除もしないし上書きもしない）。
DEDICATED_FILE_MARKER = "CODEATRIUM_HOOK_MARKER"

# install 済みコマンドの uninstall 判定に使う action 語。batch_limit や loci の
# フルパスが install 時から変わっていても、再インストール無しで正しく検出・
# 削除できるよう、コマンド文字列の完全一致ではなく action 語の部分一致にする
# （旧 CodexHooks.uninstall の挙動を踏襲）。
_MANAGED_ACTIONS = ("index", "server", "distill", "prime")


def _is_managed_command(command: str) -> bool:
    return "loci" in command and any(action in command for action in _MANAGED_ACTIONS)


@dataclass(frozen=True)
class MergedJsonEvent:
    """1つの lifecycle イベントを harness ネイティブの hook イベント + matcher に対応付ける。"""

    native_event: str
    matcher: str | None
    commands: tuple[str, ...]


class MergedJsonHookWriter:
    """共有 JSON hook 設定ファイルへコマンド単位でマージ/削除する（codex, grok が使用）。"""

    def __init__(self, harness: str, events: Sequence[MergedJsonEvent]) -> None:
        self._harness = harness
        self._events = tuple(events)

    def install(self, settings_path: Path) -> tuple[bool, str]:
        from codeatrium.hooks import _write_settings

        settings: dict[str, Any] = {}
        if settings_path.exists():
            with settings_path.open(encoding="utf-8") as stream:
                settings = json.load(stream)
        hooks = settings.setdefault("hooks", {})
        changed = False
        for spec in self._events:
            entries: list[dict[str, Any]] = hooks.setdefault(spec.native_event, [])
            target: dict[str, Any] | None = next(
                (entry for entry in entries if entry.get("matcher") == spec.matcher),
                None,
            )
            if target is None:
                target = {"hooks": []}
                if spec.matcher is not None:
                    target["matcher"] = spec.matcher
                entries.append(target)
            installed = {
                hook.get("command") for entry in entries for hook in entry.get("hooks", [])
            }
            for command in spec.commands:
                if command not in installed:
                    target["hooks"].append({"type": "command", "command": command})
                    changed = True
        if not changed:
            return False, f"{self._harness} hooks already up to date."
        _write_settings(settings_path, settings)
        return True, f"Hooks installed: {settings_path}"

    def uninstall(self, settings_path: Path) -> tuple[bool, str]:
        from codeatrium.hooks import _write_settings

        if not settings_path.exists():
            return False, f"No {self._harness} hooks file found. Nothing to uninstall."
        with settings_path.open(encoding="utf-8") as stream:
            settings: dict[str, Any] = json.load(stream)
        hooks = settings.get("hooks", {})
        changed = False
        for spec in self._events:
            entries = hooks.get(spec.native_event, [])
            for entry in entries:
                original = entry.get("hooks", [])
                filtered = [
                    hook
                    for hook in original
                    if not _is_managed_command(hook.get("command", ""))
                ]
                if filtered != original:
                    changed = True
                entry["hooks"] = filtered
            remaining = [entry for entry in entries if entry.get("hooks")]
            if remaining:
                hooks[spec.native_event] = remaining
            elif spec.native_event in hooks:
                del hooks[spec.native_event]
        if not changed:
            return (
                False,
                f"No codeatrium {self._harness} hooks found. Nothing to uninstall.",
            )
        if not hooks and "hooks" in settings:
            del settings["hooks"]
        _write_settings(settings_path, settings)
        return True, f"Hooks uninstalled: {settings_path}"


class DedicatedFileWriter:
    """自動検出ディレクトリへ専用ファイル1本を生成する（omp-pi, opencode が使用）。

    再インストールは上書き、アンインストールは `DEDICATED_FILE_MARKER` を含む
    ファイルだけを削除する。マーカーの無いファイル（他ツール/ユーザー自身が
    置いたファイル）は install/uninstall のどちらでも一切変更しない。
    """

    def __init__(
        self,
        harness: str,
        target_path: Path,
        render: Callable[[LifecycleCommands], str],
    ) -> None:
        self._harness = harness
        self._target_path = target_path
        self._render = render

    def install(self, commands: LifecycleCommands) -> tuple[bool, str]:
        content = self._render(commands)
        assert DEDICATED_FILE_MARKER in content, (
            "rendered hook file is missing the ownership marker"
        )
        if self._target_path.exists():
            existing = self._target_path.read_text(encoding="utf-8")
            if existing == content:
                return False, f"{self._harness} hooks already up to date."
            if DEDICATED_FILE_MARKER not in existing:
                return False, (
                    f"Refusing to overwrite {self._target_path}: "
                    "not managed by codeatrium."
                )
        self._target_path.parent.mkdir(parents=True, exist_ok=True)
        self._target_path.write_text(content, encoding="utf-8")
        return True, f"Hooks installed: {self._target_path}"

    def uninstall(self) -> tuple[bool, str]:
        if not self._target_path.exists():
            return False, f"No {self._harness} hook file found. Nothing to uninstall."
        existing = self._target_path.read_text(encoding="utf-8")
        if DEDICATED_FILE_MARKER not in existing:
            return False, (
                f"{self._target_path} is not managed by codeatrium; leaving it in place."
            )
        self._target_path.unlink()
        return True, f"Hooks uninstalled: {self._target_path}"
