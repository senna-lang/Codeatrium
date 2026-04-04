"""Claude Code hook 設定の JSON 操作ロジック"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from codeatrium.config import DEFAULT_DISTILL_BATCH_LIMIT
from codeatrium.paths import loci_bin


def install_hooks(batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT) -> tuple[bool, str]:
    """Claude Code の Stop / SessionStart フックに loci を登録する。

    Returns: (changed, message) — 変更の有無と結果メッセージ
    """
    settings_path = Path.home() / ".claude" / "settings.json"

    if settings_path.exists():
        with settings_path.open() as f:
            settings: dict[str, Any] = json.load(f)
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    loci = loci_bin()
    index_cmd = f"{loci} index"
    distill_cmd = f"nohup {loci} distill --limit {batch_limit} > /dev/null 2>&1 &"
    server_cmd = f"nohup {loci} server start > /dev/null 2>&1 &"
    prime_cmd = f"{loci} prime"
    changed = False

    # --- Stop hook: loci index (async: true) ---
    stop_hooks: list[dict[str, Any]] = hooks.setdefault("Stop", [])
    stop_installed = False
    for entry in stop_hooks:
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "index" in h.get("command", ""):
                stop_installed = True
                if h.get("command") != index_cmd or not h.get("async"):
                    h["command"] = index_cmd
                    h["async"] = True
                    h.pop("nohup", None)
                    changed = True
    if not stop_installed:
        stop_hooks.append(
            {"hooks": [{"type": "command", "command": index_cmd, "async": True}]}
        )
        changed = True

    # --- SessionStart hook: loci server start + loci distill (nohup detach) ---
    session_start_hooks: list[dict[str, Any]] = hooks.setdefault("SessionStart", [])

    server_start_installed = False
    for entry in session_start_hooks:
        if entry.get("matcher") != "startup|clear|resume|compact":
            continue
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "server" in h.get("command", ""):
                server_start_installed = True
                if h.get("command") != server_cmd:
                    h["command"] = server_cmd
                    changed = True

    session_start_installed = False
    for entry in session_start_hooks:
        if entry.get("matcher") != "startup|clear|resume|compact":
            continue
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "distill" in h.get("command", ""):
                session_start_installed = True
                if h.get("command") != distill_cmd:
                    h["command"] = distill_cmd
                    changed = True

    if not server_start_installed or not session_start_installed:
        target_entry = next(
            (
                e
                for e in session_start_hooks
                if e.get("matcher") == "startup|clear|resume|compact"
            ),
            None,
        )
        if target_entry is None:
            target_entry = {"matcher": "startup|clear|resume|compact", "hooks": []}
            session_start_hooks.append(target_entry)
        hooks_list = cast(list[dict[str, Any]], target_entry["hooks"])
        if not server_start_installed:
            hooks_list.append({"type": "command", "command": server_cmd})
            changed = True
        if not session_start_installed:
            hooks_list.append({"type": "command", "command": distill_cmd})
            changed = True

    # --- SessionStart hook: loci prime (blocking, stdout をコンテキストに注入) ---
    prime_installed = False
    for entry in session_start_hooks:
        if entry.get("matcher") != "startup|clear|resume|compact":
            continue
        for h in entry.get("hooks", []):
            if "loci" in h.get("command", "") and "prime" in h.get("command", ""):
                prime_installed = True
                if h.get("command") != prime_cmd:
                    h["command"] = prime_cmd
                    changed = True

    if not prime_installed:
        target_entry = next(
            (
                e
                for e in session_start_hooks
                if e.get("matcher") == "startup|clear|resume|compact"
            ),
            None,
        )
        if target_entry is None:
            target_entry = {"matcher": "startup|clear|resume|compact", "hooks": []}
            session_start_hooks.append(target_entry)
        cast(list[dict[str, Any]], target_entry["hooks"]).append(
            {"type": "command", "command": prime_cmd}
        )
        changed = True

    # 古い SessionEnd の loci distill エントリがあれば削除
    if "SessionEnd" in hooks:
        hooks["SessionEnd"] = [
            entry
            for entry in hooks["SessionEnd"]
            if not any(
                "loci" in h.get("command", "") and "distill" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        if not hooks["SessionEnd"]:
            del hooks["SessionEnd"]
        changed = True

    if not changed:
        return False, "Hooks already up to date."

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

    lines = [
        f"Hooks installed: {settings_path}",
        f"  Stop (async):       {index_cmd}",
        f"  SessionStart:       {server_cmd}",
        f"  SessionStart:       {distill_cmd}",
        f"  SessionStart:       {prime_cmd}",
        "  (matcher: startup|clear|resume|compact)",
    ]
    return True, "\n".join(lines)
