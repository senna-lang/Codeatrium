"""Harness lifecycle adapters for native hooks and explicit fallbacks.

`lifecycle_commands()`（`adapters/harness/lifecycle.py`）が「どの event で何を
実行するか」の唯一の正規情報源。ここでは harness ごとに「どこへ・どう書くか」
だけを実装する。書き込み先は2つの「ファイル所有モデル」に分類される
（design doc §3, `adapters/harness/hook_writers.py`）:

- 共有 JSON 設定ファイルへのマージ: claude（`codeatrium.hooks` の専用実装）,
  codex, grok（後者2つは `MergedJsonHookWriter` を共有）
- 専用ファイル1本の生成（自動検出ディレクトリ）: omp-pi, opencode
  （`DedicatedFileWriter` を共有）

`FallbackHooks` は6つ目以降の未知 harness のための安全網として維持する
（`hooks_for` からは現在到達しないが、クラス自体は削除しない）。
"""

from __future__ import annotations

import json
from pathlib import Path

from codeatrium.adapters.harness.hook_writers import (
    DedicatedFileWriter,
    MergedJsonEvent,
    MergedJsonHookWriter,
)
from codeatrium.adapters.harness.lifecycle import LifecycleCommands, lifecycle_commands
from codeatrium.config import DEFAULT_DISTILL_BATCH_LIMIT
from codeatrium.core.ports import Hooks


class ClaudeHooks:
    """Claude Code's native settings.json hook integration."""

    def __init__(self, batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> None:
        self._batch_limit = batch_limit

    def install(self, project_root: Path) -> tuple[bool, str]:
        del project_root
        from codeatrium.hooks import install_hooks

        return install_hooks(batch_limit=self._batch_limit)

    def uninstall(self) -> tuple[bool, str]:
        from codeatrium.hooks import uninstall_hooks

        return uninstall_hooks()

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return (
            "Claude Code uses native hooks; "
            "run `loci hook install --harness claude`."
        )


class CodexHooks:
    """Codex CLI's native ~/.codex/hooks.json integration."""

    def __init__(self, batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> None:
        self._batch_limit = batch_limit

    def _settings_path(self) -> Path:
        return Path.home() / ".codex" / "hooks.json"

    def _writer(self) -> MergedJsonHookWriter:
        commands = lifecycle_commands("codex", self._batch_limit)
        server_cmd, distill_cmd, prime_cmd = commands.on_session_start
        matcher = "startup|clear|compact"
        return MergedJsonHookWriter(
            "codex",
            (
                MergedJsonEvent("Stop", None, (commands.on_turn_end,)),
                MergedJsonEvent(
                    "SessionStart", matcher, (server_cmd, distill_cmd, prime_cmd)
                ),
            ),
        )

    def install(self, project_root: Path) -> tuple[bool, str]:
        del project_root
        return self._writer().install(self._settings_path())

    def uninstall(self) -> tuple[bool, str]:
        return self._writer().uninstall(self._settings_path())

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return "Codex supports native hooks; run `loci hook install --harness codex`."


class GrokHooks:
    """Grok CLI's native ~/.grok/hooks/*.json hook integration.

    `~/.grok/hooks/*.json` はディレクトリ自動検出だが、各ファイルの中身は
    claude/codex と同じ `{"hooks": {Event: [...]}}` マージ形式（実機確認:
    `resumex.json`, `herdr.json`）。他ツールとファイルを共有しないので、
    codeatrium 専用の `codeatrium.json` 1本に `MergedJsonHookWriter` で書く。

    Stop/SessionStart/PostCompact は grok バイナリに同梱されたドキュメント
    （`grok --help` 経由で参照可能な hooks リファレンス文字列。イベント表に
    `SessionStart`/`UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop`/
    `Notification`/`SessionEnd`/`PreCompact`/`PostCompact` 等が明記され、
    サンプル設定に `"Stop": [{"hooks": [{"type": "command", ...}]}]` の形が
    そのまま掲載されている）で実証済み。ローカルの `~/.grok/hooks/*.json` は
    `SessionStart` のみ確認済みだったが、この同梱ドキュメントにより `Stop` も
    実際に登録可能なイベントであると確認できた。
    """

    def __init__(self, batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> None:
        self._batch_limit = batch_limit

    def _settings_path(self) -> Path:
        return Path.home() / ".grok" / "hooks" / "codeatrium.json"

    def _writer(self) -> MergedJsonHookWriter:
        commands = lifecycle_commands("grok", self._batch_limit)
        server_cmd, distill_cmd, prime_cmd = commands.on_session_start
        return MergedJsonHookWriter(
            "grok",
            (
                MergedJsonEvent("Stop", None, (commands.on_turn_end,)),
                MergedJsonEvent(
                    "SessionStart", None, (server_cmd, distill_cmd, prime_cmd)
                ),
                MergedJsonEvent("PostCompact", None, (commands.on_compact,)),
            ),
        )

    def install(self, project_root: Path) -> tuple[bool, str]:
        del project_root
        return self._writer().install(self._settings_path())

    def uninstall(self) -> tuple[bool, str]:
        return self._writer().uninstall(self._settings_path())

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return "Grok supports native hooks; run `loci hook install --harness grok`."


def _run_js_snippet() -> str:
    """omp-pi/opencode の生成ファイルで共有する、コマンドを1本実行する JS 関数。"""
    return (
        "function run(command, cwd) {\n"
        "\tconst child = exec(command, cwd ? { cwd } : undefined, () => {});\n"
        "\tif (child.stdin) child.stdin.end();\n"
        "}"
    )


def _dedicated_file_header(harness_note: str) -> str:
    from codeatrium.adapters.harness.hook_writers import DEDICATED_FILE_MARKER

    return (
        "// installed by codeatrium\n"
        "// managed by codeatrium; reinstalling overwrites this file.\n"
        "// add custom hooks/plugins beside this file instead of editing it.\n"
        f"// {DEDICATED_FILE_MARKER}\n"
        "//\n"
        f"{harness_note}"
    )


def _render_omp_pi_extension(commands: LifecycleCommands) -> str:
    """`~/.omp/agent/extensions/codeatrium.ts` の中身を組み立てる。

    観測済み omp-pi イベント（design doc §2）のうち、ターン終了相当は
    `agent_end`、session 開始相当は `session_start`/`session_switch`。
    compact 相当のイベントは実機で観測されていないため登録しない。
    """
    session_start_cmd = " ".join(commands.on_session_start)
    header = _dedicated_file_header(
        "// Wires codeatrium's lifecycle triggers into OMP's own agent/session\n"
        "// events: agent_end (turn end) -> loci index, session_start /\n"
        "// session_switch (session start) -> server start + distill + prime.\n"
    )
    return (
        f"{header}"
        'import { exec } from "node:child_process";\n'
        "\n"
        f"{_run_js_snippet()}\n"
        "\n"
        "export default function (pi) {\n"
        '\tpi.on("agent_end", (_event, ctx) => {\n'
        f"\t\trun({json.dumps(commands.on_turn_end)}, ctx && ctx.cwd);\n"
        "\t});\n"
        '\tpi.on("session_start", (_event, ctx) => {\n'
        f"\t\trun({json.dumps(session_start_cmd)}, ctx && ctx.cwd);\n"
        "\t});\n"
        '\tpi.on("session_switch", (_event, ctx) => {\n'
        f"\t\trun({json.dumps(session_start_cmd)}, ctx && ctx.cwd);\n"
        "\t});\n"
        "}\n"
    )


def _render_opencode_plugin(commands: LifecycleCommands) -> str:
    """`~/.config/opencode/plugins/codeatrium.ts` の中身を組み立てる。

    観測済み opencode イベント（design doc §2）: `session.idle` が Stop 相当
    （ターン終了）、`session.created` が session 開始、`session.compacted` が
    compact 完了。
    """
    session_start_cmd = " ".join(commands.on_session_start)
    header = _dedicated_file_header(
        "// Wires codeatrium's lifecycle triggers into OpenCode's session\n"
        "// events: session.idle (turn end) -> loci index, session.created\n"
        "// (session start) -> server start + distill + prime,\n"
        "// session.compacted -> loci prime.\n"
    )
    return (
        f"{header}"
        'import { exec } from "node:child_process";\n'
        "\n"
        f"{_run_js_snippet()}\n"
        "\n"
        "export const CodeatriumHook = async ({ directory }) => ({\n"
        "\tevent: async ({ event }) => {\n"
        "\t\tswitch (event?.type) {\n"
        '\t\t\tcase "session.idle":\n'
        f"\t\t\t\trun({json.dumps(commands.on_turn_end)}, directory);\n"
        "\t\t\t\tbreak;\n"
        '\t\t\tcase "session.created":\n'
        f"\t\t\t\trun({json.dumps(session_start_cmd)}, directory);\n"
        "\t\t\t\tbreak;\n"
        '\t\t\tcase "session.compacted":\n'
        f"\t\t\t\trun({json.dumps(commands.on_compact)}, directory);\n"
        "\t\t\t\tbreak;\n"
        "\t\t}\n"
        "\t},\n"
        "});\n"
    )


class OmpPiHooks:
    """omp-pi's native ~/.omp/agent/extensions/*.ts plugin integration."""

    def __init__(self, batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> None:
        self._batch_limit = batch_limit

    def _target_path(self) -> Path:
        return Path.home() / ".omp" / "agent" / "extensions" / "codeatrium.ts"

    def _writer(self) -> DedicatedFileWriter:
        return DedicatedFileWriter(
            "omp-pi", self._target_path(), _render_omp_pi_extension
        )

    def install(self, project_root: Path) -> tuple[bool, str]:
        del project_root
        commands = lifecycle_commands("omp-pi", self._batch_limit)
        return self._writer().install(commands)

    def uninstall(self) -> tuple[bool, str]:
        return self._writer().uninstall()

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return "omp-pi supports native hooks; run `loci hook install --harness omp-pi`."


class OpenCodeHooks:
    """OpenCode's native ~/.config/opencode/plugins/*.ts plugin integration."""

    def __init__(self, batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> None:
        self._batch_limit = batch_limit

    def _target_path(self) -> Path:
        return Path.home() / ".config" / "opencode" / "plugins" / "codeatrium.ts"

    def _writer(self) -> DedicatedFileWriter:
        return DedicatedFileWriter(
            "opencode", self._target_path(), _render_opencode_plugin
        )

    def install(self, project_root: Path) -> tuple[bool, str]:
        del project_root
        commands = lifecycle_commands("opencode", self._batch_limit)
        return self._writer().install(commands)

    def uninstall(self) -> tuple[bool, str]:
        return self._writer().uninstall()

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return (
            "opencode supports native hooks; "
            "run `loci hook install --harness opencode`."
        )


class FallbackHooks:
    """Lifecycle instructions for harnesses without supported native hooks.

    未知の6つ目以降の harness のための安全網。既知の5 harness
    （claude/codex/omp-pi/opencode/grok）は全てネイティブ実装を持つため
    `hooks_for` からは現在到達しないが、クラス自体は将来のために維持する。
    """

    def __init__(self, harness: str) -> None:
        self._harness = harness

    def install(self, project_root: Path) -> tuple[bool, str]:
        return False, self.fallback_recipe(project_root)

    def uninstall(self) -> tuple[bool, str]:
        return (
            False,
            f"{self._harness} has no native codeatrium hooks to uninstall.",
        )

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return "\n".join(
            (
                f"{self._harness} has no supported native lifecycle hooks.",
                f"After each turn: `loci index --harness {self._harness}`",
                (
                    "At session start: `loci server start`, `loci distill`, "
                    "then `loci prime`."
                ),
                "After compaction: `loci prime`.",
            )
        )


def hooks_for(
    harness: str, batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT
) -> Hooks:
    """Return the lifecycle capability for a supported harness."""
    if harness == "claude":
        return ClaudeHooks(batch_limit)
    if harness == "codex":
        return CodexHooks(batch_limit)
    if harness == "omp-pi":
        return OmpPiHooks(batch_limit)
    if harness == "opencode":
        return OpenCodeHooks(batch_limit)
    if harness == "grok":
        return GrokHooks(batch_limit)
    raise ValueError(f"Unsupported harness: {harness}")
