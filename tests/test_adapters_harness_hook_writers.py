"""MergedJsonHookWriter / DedicatedFileWriter の共通契約を検証する（design doc §4.2）。

harness アダプター（Codex/Grok/OmpPi/OpenCode）を経由せず、writer 自体の
振る舞い（冪等性・他ツールとの共存・マーカーによる所有権判定）を直接検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeatrium.adapters.harness.hook_writers import (
    DEDICATED_FILE_MARKER,
    DedicatedFileWriter,
    MergedJsonEvent,
    MergedJsonHookWriter,
)

# ---- MergedJsonHookWriter ----


def _writer(commands: tuple[str, ...] = ("cmd-a", "cmd-b")) -> MergedJsonHookWriter:
    return MergedJsonHookWriter(
        "test-harness",
        (MergedJsonEvent("Stop", None, (commands[0],)),
         MergedJsonEvent("SessionStart", "startup", commands[1:])),
    )


def test_merged_json_install_creates_file_from_scratch(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    changed, message = _writer().install(target)

    assert changed is True
    assert "installed" in message.lower()
    data = json.loads(target.read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "cmd-a"
    assert data["hooks"]["SessionStart"][0]["matcher"] == "startup"


def test_merged_json_install_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    writer = _writer()
    writer.install(target)
    changed, message = writer.install(target)

    assert changed is False
    assert "up to date" in message


def test_merged_json_install_preserves_unrelated_hooks(tmp_path: Path) -> None:
    """共有ファイルの他ツール/ユーザーの hook エントリを消さない。"""
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "my-tool run"}]}
                    ]
                }
            }
        )
    )
    _writer().install(target)

    data = json.loads(target.read_text())
    stop_commands = [
        h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]
    ]
    assert "my-tool run" in stop_commands
    assert "cmd-a" in stop_commands


def test_merged_json_uninstall_removes_managed_commands_only(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    writer = MergedJsonHookWriter(
        "test-harness",
        (
            MergedJsonEvent("Stop", None, ("/path/to/loci index --harness x",)),
            MergedJsonEvent("SessionStart", None, ("/path/to/loci prime",)),
        ),
    )
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "/path/to/loci index --harness x"},
                                {"type": "command", "command": "my-tool run"},
                            ]
                        }
                    ],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/path/to/loci prime"}]}
                    ],
                }
            }
        )
    )

    changed, message = writer.uninstall(target)

    assert changed is True
    data = json.loads(target.read_text())
    stop_commands = [
        h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]
    ]
    assert stop_commands == ["my-tool run"]
    assert "SessionStart" not in data["hooks"]  # 空になったキーは削除される


def test_merged_json_uninstall_matches_despite_changed_batch_limit(
    tmp_path: Path,
) -> None:
    """batch_limit が変わって distill コマンド文字列が変化しても削除できる。"""
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "nohup /x/loci distill --limit 5 > /dev/null 2>&1 &",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    writer = MergedJsonHookWriter(
        "test-harness",
        (MergedJsonEvent("SessionStart", None, ("nohup /x/loci distill --limit 99 > /dev/null 2>&1 &",)),),
    )

    changed, _ = writer.uninstall(target)

    assert changed is True
    data = json.loads(target.read_text())
    assert "hooks" not in data or not data.get("hooks", {}).get("SessionStart")


def test_merged_json_uninstall_missing_file_is_noop(tmp_path: Path) -> None:
    changed, message = _writer().uninstall(tmp_path / "missing.json")
    assert changed is False
    assert "nothing to uninstall" in message.lower()


def test_merged_json_uninstall_no_managed_hooks_is_noop(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-tool run"}]}]}}))

    changed, message = _writer().uninstall(target)

    assert changed is False
    assert "nothing to uninstall" in message.lower()


# ---- DedicatedFileWriter ----


def _render(marker: str = DEDICATED_FILE_MARKER) -> str:
    return f"// {marker}\nconsole.log('hi');\n"


def test_dedicated_file_install_writes_new_file(tmp_path: Path) -> None:
    target = tmp_path / "ext" / "codeatrium.ts"
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: _render())

    changed, message = writer.install(commands=object())  # type: ignore[arg-type]

    assert changed is True
    assert "installed" in message.lower()
    assert target.exists()
    assert DEDICATED_FILE_MARKER in target.read_text()


def test_dedicated_file_install_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "ext" / "codeatrium.ts"
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: _render())
    writer.install(commands=object())  # type: ignore[arg-type]

    changed, message = writer.install(commands=object())  # type: ignore[arg-type]

    assert changed is False
    assert "up to date" in message


def test_dedicated_file_install_refuses_to_overwrite_unmanaged_file(
    tmp_path: Path,
) -> None:
    """マーカーの無い既存ファイルは他ツール/ユーザー製とみなし上書きしない。"""
    target = tmp_path / "ext" / "codeatrium.ts"
    target.parent.mkdir(parents=True)
    target.write_text("// someone else's file\n")
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: _render())

    changed, message = writer.install(commands=object())  # type: ignore[arg-type]

    assert changed is False
    assert "not managed by codeatrium" in message
    assert target.read_text() == "// someone else's file\n"


def test_dedicated_file_uninstall_removes_marker_owned_file(tmp_path: Path) -> None:
    target = tmp_path / "ext" / "codeatrium.ts"
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: _render())
    writer.install(commands=object())  # type: ignore[arg-type]

    changed, message = writer.uninstall()

    assert changed is True
    assert not target.exists()


def test_dedicated_file_uninstall_refuses_to_delete_unmanaged_file(
    tmp_path: Path,
) -> None:
    """マーカーの無いファイルは uninstall でも削除しない（issue #28 の誤削除対策）。"""
    target = tmp_path / "ext" / "codeatrium.ts"
    target.parent.mkdir(parents=True)
    target.write_text("// someone else's file, name collides with ours\n")
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: _render())

    changed, message = writer.uninstall()

    assert changed is False
    assert "not managed by codeatrium" in message
    assert target.exists()


def test_dedicated_file_uninstall_missing_file_is_noop(tmp_path: Path) -> None:
    target = tmp_path / "ext" / "codeatrium.ts"
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: _render())

    changed, message = writer.uninstall()

    assert changed is False
    assert "nothing to uninstall" in message.lower()


def test_dedicated_file_install_rejects_render_missing_marker(tmp_path: Path) -> None:
    target = tmp_path / "ext" / "codeatrium.ts"
    writer = DedicatedFileWriter("test-harness", target, lambda _cmds: "no marker here")

    with pytest.raises(AssertionError):
        writer.install(commands=object())  # type: ignore[arg-type]
