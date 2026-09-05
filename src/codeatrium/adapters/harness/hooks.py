"""Harness lifecycle adapters for native hooks and explicit fallbacks."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from codeatrium.config import DEFAULT_DISTILL_BATCH_LIMIT
from codeatrium.core.ports import Hooks
from codeatrium.paths import loci_bin


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

    def install(self, project_root: Path) -> tuple[bool, str]:
        del project_root
        from codeatrium.hooks import _write_settings

        settings_path = Path.home() / ".codex" / "hooks.json"
        settings: dict[str, Any] = {}
        if settings_path.exists():
            with settings_path.open(encoding="utf-8") as stream:
                settings = json.load(stream)
        hooks = settings.setdefault("hooks", {})
        loci = shlex.quote(loci_bin())
        commands = (
            ("Stop", None, f"{loci} index --harness codex"),
            (
                "SessionStart",
                "startup|clear|compact",
                f"nohup {loci} server start > /dev/null 2>&1 &",
            ),
            (
                "SessionStart",
                "startup|clear|compact",
                f"nohup {loci} distill --limit {self._batch_limit} > /dev/null 2>&1 &",
            ),
            ("SessionStart", "startup|clear|compact", f"{loci} prime"),
        )
        changed = False
        for event, matcher, command in commands:
            entries = hooks.setdefault(event, [])
            target: dict[str, Any] | None = next(
                (
                    entry
                    for entry in entries
                    if entry.get("matcher") == matcher
                ),
                None,
            )
            if target is None:
                target = {"hooks": []}
                if matcher is not None:
                    target["matcher"] = matcher
                entries.append(target)
            installed = False
            for entry in entries:
                for hook in entry.get("hooks", []):
                    if hook.get("command") == command:
                        installed = True
            if not installed:
                target["hooks"].append({"type": "command", "command": command})
                changed = True
        if not changed:
            return False, "Codex hooks already up to date."
        _write_settings(settings_path, settings)
        return True, f"Hooks installed: {settings_path}"

    def uninstall(self) -> tuple[bool, str]:
        from codeatrium.hooks import _write_settings

        settings_path = Path.home() / ".codex" / "hooks.json"
        if not settings_path.exists():
            return False, "No Codex hooks.json found. Nothing to uninstall."
        with settings_path.open(encoding="utf-8") as stream:
            settings: dict[str, Any] = json.load(stream)
        hooks = settings.get("hooks", {})
        changed = False
        for event in ("Stop", "SessionStart"):
            entries = hooks.get(event, [])
            for entry in entries[:]:
                original = entry.get("hooks", [])
                entry["hooks"] = [
                    hook
                    for hook in original
                    if not (
                        "loci" in hook.get("command", "")
                        and any(
                            action in hook.get("command", "")
                            for action in ("index", "server", "distill", "prime")
                        )
                    )
                ]
                changed |= entry["hooks"] != original
            hooks[event] = [entry for entry in entries if entry.get("hooks")]
            if not hooks[event]:
                hooks.pop(event, None)
        if not changed:
            return False, "No codeatrium Codex hooks found. Nothing to uninstall."
        _write_settings(settings_path, settings)
        return True, f"Hooks uninstalled: {settings_path}"

    def fallback_recipe(self, project_root: Path) -> str:
        del project_root
        return "Codex supports native hooks; run `loci hook install --harness codex`."

class FallbackHooks:
    """Lifecycle instructions for harnesses without supported native hooks."""

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
    if harness in {"omp-pi", "opencode", "grok"}:
        return FallbackHooks(harness)
    raise ValueError(f"Unsupported harness: {harness}")
