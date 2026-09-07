"""
loci status / loci hook install のテスト

status コマンド: exchange 数・蒸留済み数・DB サイズを返す
hook install  : ~/.claude/settings.json に Stop hook を登録する
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import init_db

runner = CliRunner()


# ---- helpers ----


def _setup_db(tmp_path: Path) -> Path:
    """テスト用 DB を初期化して codeatrium ディレクトリを作成する"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    db = codeatrium_dir / "memory.db"
    init_db(db)
    return db


# ---- status ----


def test_status_not_initialized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code != 0
    assert "loci init" in result.output


def test_status_empty_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup_db(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "0" in result.output


def test_status_json_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup_db(tmp_path)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "exchanges" in data
    assert "distilled" in data
    assert "skipped" in data
    assert "pending" in data
    assert "palace_objects" in data
    assert "symbols" in data
    assert "db_size_kb" in data


def test_status_counts_exchanges(tmp_path, monkeypatch):
    import hashlib
    import sqlite3

    monkeypatch.chdir(tmp_path)
    db = _setup_db(tmp_path)

    # exchanges を2件挿入（うち1件を蒸留済みに）
    con = sqlite3.connect(db)
    ex_id1 = hashlib.sha256(b"ex1").hexdigest()
    ex_id2 = hashlib.sha256(b"ex2").hexdigest()
    conv_id = hashlib.sha256(b"conv").hexdigest()
    con.execute(
        "INSERT INTO conversations (id, source_path) VALUES (?, ?)",
        (conv_id, "/tmp/test.jsonl"),
    )
    con.execute(
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) VALUES (?, ?, ?, ?, ?, ?)",
        (ex_id1, conv_id, 0, 1, "hello world", "hi there"),
    )
    con.execute(
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content, distilled_at, distill_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ex_id2, conv_id, 2, 3, "foo bar", "baz qux", "2026-01-01T00:00:00", "distilled"),
    )
    con.commit()
    con.close()

    result = runner.invoke(app, ["status", "--json"])
    data = json.loads(result.output)
    assert data["exchanges"] == 2
    assert data["distilled"] == 1
    assert data["pending"] == 1


def test_status_shows_unconfigured_distill(tmp_path, monkeypatch):
    _setup_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "--json"])
    data = json.loads(result.output)
    assert data["distill_client"] == "unconfigured"
    assert data["distill_available"] is False


def test_status_shows_ready_distill_client(tmp_path, monkeypatch):
    _setup_db(tmp_path)
    (tmp_path / ".codeatrium" / "config.toml").write_text(
        '[distill]\nclient = "claude-cli"\n'
    )
    monkeypatch.chdir(tmp_path)

    from codeatrium.adapters.model.types import ClientStatus, ModelClient

    monkeypatch.setattr(
        "codeatrium.adapters.model.registry.check_ready",
        lambda client_id: ClientStatus(
            id="claude-cli",
            label="Claude CLI",
            state="ready",
            reason="ready",
            client=ModelClient(
                id="claude-cli",
                provider="claude",
                model="claude-haiku-4-5-20251001",
                base_url=None,
                label="Claude CLI",
            ),
        ),
    )

    result = runner.invoke(app, ["status", "--json"])
    data = json.loads(result.output)
    assert data["distill_client"] == "claude-cli"
    assert data["distill_available"] is True


# ---- hook install ----


def test_hook_install_creates_settings(tmp_path, monkeypatch):
    settings_path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 0
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert "hooks" in data
    assert "Stop" in data["hooks"]

def test_hook_install_omp_pi_writes_dedicated_extension_file(tmp_path, monkeypatch):
    """omp-pi は FallbackHooks ではなく ~/.omp/agent/extensions/*.ts へ実際に書く（issue #40）"""
    ext_path = tmp_path / ".omp" / "agent" / "extensions" / "codeatrium.ts"
    monkeypatch.setattr(
        "codeatrium.adapters.harness.hooks.Path.home", lambda: tmp_path
    )

    result = runner.invoke(app, ["hook", "install", "--harness", "omp-pi"])

    assert result.exit_code == 0
    assert ext_path.exists()
    content = ext_path.read_text()
    assert "CODEATRIUM_HOOK_MARKER" in content
    assert "loci index --harness omp-pi" in content
    assert "agent_end" in content
    assert "session_start" in content

    second = runner.invoke(app, ["hook", "install", "--harness", "omp-pi"])
    assert "already up to date" in second.output


def test_hook_uninstall_omp_pi_removes_only_marker_owned_file(tmp_path, monkeypatch):
    """マーカーの無いファイルは誤削除しない（issue #28 の共有ディレクトリ誤削除対策）"""
    ext_dir = tmp_path / ".omp" / "agent" / "extensions"
    ext_dir.mkdir(parents=True)
    other_file = ext_dir / "other-tool.ts"
    other_file.write_text("// not ours\n")
    monkeypatch.setattr(
        "codeatrium.adapters.harness.hooks.Path.home", lambda: tmp_path
    )

    runner.invoke(app, ["hook", "install", "--harness", "omp-pi"])
    result = runner.invoke(app, ["hook", "uninstall", "--harness", "omp-pi"])

    assert result.exit_code == 0
    assert not (ext_dir / "codeatrium.ts").exists()
    assert other_file.exists()  # 他ツールのファイルは無傷


def test_hook_install_opencode_writes_dedicated_plugin_file(tmp_path, monkeypatch):
    plugin_path = tmp_path / ".config" / "opencode" / "plugins" / "codeatrium.ts"
    monkeypatch.setattr(
        "codeatrium.adapters.harness.hooks.Path.home", lambda: tmp_path
    )

    result = runner.invoke(app, ["hook", "install", "--harness", "opencode"])

    assert result.exit_code == 0
    assert plugin_path.exists()
    content = plugin_path.read_text()
    assert "CODEATRIUM_HOOK_MARKER" in content
    assert "loci index --harness opencode" in content
    assert "session.idle" in content
    assert "session.created" in content
    assert "session.compacted" in content


def test_hook_install_grok_uses_native_hooks_file(tmp_path, monkeypatch):
    hooks_path = tmp_path / ".grok" / "hooks" / "codeatrium.json"
    monkeypatch.setattr(
        "codeatrium.adapters.harness.hooks.Path.home", lambda: tmp_path
    )

    result = runner.invoke(app, ["hook", "install", "--harness", "grok"])

    assert result.exit_code == 0
    data = json.loads(hooks_path.read_text())
    stop_commands = [
        hook["command"] for entry in data["hooks"]["Stop"] for hook in entry["hooks"]
    ]
    assert any("loci index --harness grok" in cmd for cmd in stop_commands)
    session_commands = [
        hook["command"]
        for entry in data["hooks"]["SessionStart"]
        for hook in entry["hooks"]
    ]
    assert any("loci server start" in cmd for cmd in session_commands)
    assert any("loci distill" in cmd for cmd in session_commands)
    assert any("loci prime" in cmd for cmd in session_commands)
    compact_commands = [
        hook["command"]
        for entry in data["hooks"]["PostCompact"]
        for hook in entry["hooks"]
    ]
    assert any("loci prime" in cmd for cmd in compact_commands)

    second = runner.invoke(app, ["hook", "install", "--harness", "grok"])
    assert "already up to date" in second.output

    uninstall_result = runner.invoke(app, ["hook", "uninstall", "--harness", "grok"])
    assert uninstall_result.exit_code == 0
    data_after = json.loads(hooks_path.read_text())
    assert "hooks" not in data_after or not data_after["hooks"]


def test_hook_install_codex_uses_native_hooks_file(tmp_path, monkeypatch):
    hooks_path = tmp_path / ".codex" / "hooks.json"
    monkeypatch.setattr(
        "codeatrium.adapters.harness.hooks.Path.home", lambda: tmp_path
    )

    result = runner.invoke(app, ["hook", "install", "--harness", "codex"])

    assert result.exit_code == 0
    data = json.loads(hooks_path.read_text())
    stop_commands = [
        hook["command"]
        for entry in data["hooks"]["Stop"]
        for hook in entry["hooks"]
    ]
    assert any("loci index --harness codex" in command for command in stop_commands)
    session_commands = [
        hook["command"]
        for entry in data["hooks"]["SessionStart"]
        for hook in entry["hooks"]
    ]
    assert any("loci server start" in command for command in session_commands)
    assert any("loci distill" in command for command in session_commands)
    assert any("loci prime" in command for command in session_commands)

    second = runner.invoke(app, ["hook", "install", "--harness", "codex"])
    assert "already up to date" in second.output

def test_hook_install_adds_command(tmp_path, monkeypatch):
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    runner.invoke(app, ["hook", "install"])
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())

    # Stop hook: loci index (async: true)
    stop_commands = [
        h for entry in data["hooks"]["Stop"] for h in entry.get("hooks", [])
    ]
    assert any("loci index" in h.get("command", "") for h in stop_commands)
    # lifecycle_commands("claude", ...) が唯一の正規情報源になった（issue #40
    # レビュー指摘: ClaudeHooks も lifecycle_commands を経由すること）
    assert any(
        "loci index --harness claude" in h.get("command", "") for h in stop_commands
    )
    assert all(
        h.get("async") is True
        for h in stop_commands
        if "loci index" in h.get("command", "")
    )

    # SessionStart hook: loci distill (matcher: startup|clear|resume|compact)
    session_start_entries = data["hooks"]["SessionStart"]
    assert any(
        entry.get("matcher") == "startup|clear|resume|compact"
        for entry in session_start_entries
    )
    session_start_commands = [
        h for entry in session_start_entries for h in entry.get("hooks", [])
    ]
    assert any("loci distill" in h.get("command", "") for h in session_start_commands)

    # SessionStart hook: loci prime
    assert any("loci prime" in h.get("command", "") for h in session_start_commands)


def test_hook_install_claude_command_tracks_lifecycle_commands_source(
    tmp_path, monkeypatch
):
    """install_hooks() が lifecycle_commands() を経由することを直接確認する
    （codeatrium.hooks が独立してコマンド文字列を再構築していないことの回帰テスト）。
    """
    from codeatrium.hooks import install_hooks

    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "codeatrium.adapters.harness.lifecycle.loci_bin",
        lambda: "/fake/venv/bin/loci",
    )

    install_hooks(batch_limit=7)

    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    stop_commands = [
        h["command"] for entry in data["hooks"]["Stop"] for h in entry.get("hooks", [])
    ]
    session_commands = [
        h["command"]
        for entry in data["hooks"]["SessionStart"]
        for h in entry.get("hooks", [])
    ]
    assert any(
        cmd == "/fake/venv/bin/loci index --harness claude" for cmd in stop_commands
    )
    assert any("--limit 7" in cmd for cmd in session_commands)


def test_hook_install_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    runner.invoke(app, ["hook", "install"])
    runner.invoke(app, ["hook", "install"])
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    # 2回実行しても hook は1件のみ
    all_hooks = [h for entry in data["hooks"]["Stop"] for h in entry.get("hooks", [])]
    loci_hooks = [h for h in all_hooks if "loci index" in h.get("command", "")]
    assert len(loci_hooks) == 1


def test_hook_install_prime_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    runner.invoke(app, ["hook", "install"])
    runner.invoke(app, ["hook", "install"])
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    session_start_commands = [
        h
        for entry in data["hooks"]["SessionStart"]
        for h in entry.get("hooks", [])
    ]
    prime_hooks = [h for h in session_start_commands if "loci prime" in h.get("command", "")]
    assert len(prime_hooks) == 1


def test_prime_outputs_instructions(tmp_path, monkeypatch):
    _setup_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["prime"])
    assert result.exit_code == 0
    assert "loci search" in result.output
    assert "loci context" in result.output
    assert "loci show" in result.output


def test_prime_outputs_branch_usage(tmp_path, monkeypatch):
    _setup_db(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["prime"])
    assert result.exit_code == 0
    assert "--branch" in result.output
    assert "loci context --branch" in result.output


def test_prime_silent_when_uninitialized(tmp_path, monkeypatch):
    """.codeatrium/ がないディレクトリでは何も出力せず exit 0 で抜ける"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["prime"])
    assert result.exit_code == 0
    assert result.output == ""


def test_hook_install_merges_existing_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"model": "opus"}))

    runner.invoke(app, ["hook", "install"])
    data = json.loads(settings_path.read_text())
    # 既存設定が保持されている
    assert data.get("model") == "opus"
    assert "hooks" in data


# ---- hook install atomic + backup ----


def test_write_settings_atomic_bak(tmp_path, monkeypatch):
    """install 時に既存 settings.json を .bak にバックアップする"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"model": "opus"}))

    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 0

    bak_path = settings_path.with_suffix(".json.bak")
    assert bak_path.exists()
    bak_data = json.loads(bak_path.read_text())
    assert bak_data.get("model") == "opus"


def test_write_settings_failure_keeps_original_intact(tmp_path, monkeypatch):
    """書き込み失敗(例外注入)時に元 settings.json が無傷であることを確認"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    initial_content = {"model": "opus", "existing": True}
    settings_path.write_text(json.dumps(initial_content))

    # os.replace を例外を投げる mock に patch する
    with patch("codeatrium.hooks.os.replace", side_effect=OSError("disk full")):
        from codeatrium.hooks import install_hooks
        # install_hooks() が OSError を送出することを確認
        with pytest.raises(OSError):
            install_hooks()

    # 元の settings.json が無傷であることを assert
    assert settings_path.exists()
    original_data = json.loads(settings_path.read_text())
    assert original_data == initial_content


def test_write_settings_atomic_no_bak_when_missing(tmp_path, monkeypatch):
    """settings.json が存在しない場合は .bak は作成されない"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"

    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 0

    bak_path = settings_path.with_suffix(".json.bak")
    assert not bak_path.exists()


# ---- hook uninstall ----


def test_hook_uninstall_removes_codeatrium_hooks(tmp_path, monkeypatch):
    """uninstall は codeatrium フックを削除する"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"

    runner.invoke(app, ["hook", "install"])
    result = runner.invoke(app, ["hook", "uninstall"])
    assert result.exit_code == 0

    data = json.loads(settings_path.read_text())
    # hooks がないか、Stop/SessionStart/SessionEnd に loci コマンドを含むエントリが無いこと
    if "hooks" in data:
        for hook_type in ["Stop", "SessionStart", "SessionEnd"]:
            if hook_type in data["hooks"]:
                entries = data["hooks"][hook_type]
                for entry in entries:
                    for h in entry.get("hooks", []):
                        assert "loci" not in h.get("command", "")


def test_hook_uninstall_preserves_user_hooks(tmp_path, monkeypatch):
    """uninstall はユーザーフックを保持する"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "my-tool run"}]}]
            }
        })
    )

    runner.invoke(app, ["hook", "install"])
    runner.invoke(app, ["hook", "uninstall"])

    data = json.loads(settings_path.read_text())
    assert "Stop" in data["hooks"]
    stop_entries = data["hooks"]["Stop"]
    all_commands = [h for entry in stop_entries for h in entry.get("hooks", [])]
    assert any("my-tool run" in h.get("command", "") for h in all_commands)


def test_hook_uninstall_idempotent(tmp_path, monkeypatch):
    """uninstall は複数回実行しても安全（べき等）"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)

    # install なしで直接 uninstall
    result1 = runner.invoke(app, ["hook", "uninstall"])
    assert result1.exit_code == 0
    assert "Nothing to uninstall" in result1.output or "No" in result1.output

    # 2回目も同じ
    result2 = runner.invoke(app, ["hook", "uninstall"])
    assert result2.exit_code == 0
    assert "Nothing to uninstall" in result2.output or "No" in result2.output


def test_hook_uninstall_empty_matcher_removed(tmp_path, monkeypatch):
    """uninstall 後、空の matcher を持つエントリは削除される"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"

    runner.invoke(app, ["hook", "install"])
    runner.invoke(app, ["hook", "uninstall"])

    data = json.loads(settings_path.read_text())
    if "hooks" in data and "SessionStart" in data["hooks"]:
        entries = data["hooks"]["SessionStart"]
        for entry in entries:
            # 各エントリは空でない hooks を持つこと
            hooks = entry.get("hooks", [])
            assert len(hooks) > 0


# ---- issue #28: matcher差分検知・loci_bin prefix一致・不正JSON・backup rotation ----


def test_hook_install_detects_loci_hooks_under_non_canonical_matcher(
    tmp_path, monkeypatch
):
    """SessionStart の matcher がカノニカル文字列と異なっていても既存の loci
    フックを検知し、重複登録しない（issue #28: matcher差分での重複登録）。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "codeatrium.adapters.harness.lifecycle.loci_bin",
        lambda: "/fake/venv/bin/loci",
    )
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/fake/venv/bin/loci index --harness claude",
                                    "async": True,
                                }
                            ]
                        }
                    ],
                    "SessionStart": [
                        {
                            # ユーザーがカスタマイズした matcher（正準文字列と異なる）
                            "matcher": "startup|resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "nohup /fake/venv/bin/loci server start > /dev/null 2>&1 &",
                                },
                                {
                                    "type": "command",
                                    "command": "nohup /fake/venv/bin/loci distill --limit 20 > /dev/null 2>&1 &",
                                },
                                {
                                    "type": "command",
                                    "command": "/fake/venv/bin/loci prime",
                                },
                            ],
                        }
                    ],
                }
            }
        )
    )

    from codeatrium.hooks import install_hooks

    changed, _message = install_hooks(batch_limit=20)

    assert changed is False  # 既に登録済みとして検知され、重複追加されない
    data = json.loads(settings_path.read_text())
    session_start_entries = data["hooks"]["SessionStart"]
    # カスタム matcher のエントリがそのまま残り、正準 matcher の新規エントリは
    # 作られない（= 重複登録されていない）
    assert len(session_start_entries) == 1
    assert session_start_entries[0]["matcher"] == "startup|resume"
    assert len(session_start_entries[0]["hooks"]) == 3


def test_hook_uninstall_does_not_delete_unrelated_command_with_loci_substring(
    tmp_path, monkeypatch
):
    """コマンドパスにたまたま "loci" を含むだけの無関係なユーザーコマンドを
    誤って削除しない（issue #28: _is_loci 部分一致の脆弱性、loci_bin prefix一致に）。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    # "loci" と "index" の両方を含むが、実体は無関係な
                                    # ユーザースクリプト
                                    "command": "/home/user/tools/my-loci-backup.sh --index",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    from codeatrium.hooks import uninstall_hooks

    changed, message = uninstall_hooks()

    assert changed is False
    assert "Nothing to uninstall" in message or "No codeatrium" in message
    data = json.loads(settings_path.read_text())
    stop_commands = [
        h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]
    ]
    assert "/home/user/tools/my-loci-backup.sh --index" in stop_commands

def test_hook_uninstall_does_not_delete_relative_bin_loci_command(tmp_path, monkeypatch):
    """絶対パスではない `bin/loci` は codeatrium が生成する hook ではないため、
    action 名が同居していてもユーザーコマンドとして残す。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bin/loci index --harness claude",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    from codeatrium.hooks import uninstall_hooks

    changed, _message = uninstall_hooks()

    assert changed is False
    data = json.loads(settings_path.read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == (
        "bin/loci index --harness claude"
    )


def test_hook_uninstall_removes_hooks_installed_from_different_venv(
    tmp_path, monkeypatch
):
    """install 時と異なる virtualenv の loci バイナリで登録された hook でも、
    現在の環境から `loci hook uninstall` すれば安全に検知・削除できる
    （PR #54 レビュー: `loci_bin()` の絶対パス厳密一致だけに頼ると、別 venv で
    インストール後に uninstall した際に既存 hook を取りこぼす）。同時に、
    無関係なユーザーコマンドは誤って削除されない（広すぎる部分文字列判定への
    後退防止）。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    # 現在の環境の loci_bin() とは異なる、別 virtualenv (/old-venv) の loci で
    # インストールされた hook を模す。あわせて "loci" を含むだけの無関係な
    # ユーザーコマンドも同居させ、それが誤削除されないことも検証する。
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/old-venv/bin/loci index --harness claude",
                                    "async": True,
                                },
                                {
                                    "type": "command",
                                    "command": "/home/user/tools/my-loci-backup.sh --index",
                                },
                            ]
                        }
                    ],
                    "SessionStart": [
                        {
                            "matcher": "startup|clear|resume|compact",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "nohup /old-venv/bin/loci server start > /dev/null 2>&1 &",
                                },
                                {
                                    "type": "command",
                                    "command": "nohup /old-venv/bin/loci distill --limit 20 > /dev/null 2>&1 &",
                                },
                                {
                                    "type": "command",
                                    "command": "/old-venv/bin/loci prime",
                                },
                            ],
                        }
                    ],
                }
            }
        )
    )

    from codeatrium.hooks import uninstall_hooks

    changed, message = uninstall_hooks()

    assert changed is True
    assert "Hooks uninstalled" in message
    data = json.loads(settings_path.read_text())
    stop_commands = [
        h["command"] for entry in data["hooks"].get("Stop", []) for h in entry["hooks"]
    ]
    # 別 venv の loci hook は削除される
    assert "/old-venv/bin/loci index --harness claude" not in stop_commands
    # 無関係なユーザーコマンド（"loci" を含むだけ）は残る
    assert "/home/user/tools/my-loci-backup.sh --index" in stop_commands
    # SessionStart は全エントリが loci hook のみだったため丸ごと消える
    assert "SessionStart" not in data.get("hooks", {})


def test_install_hooks_malformed_json_raises_actionable_error(tmp_path, monkeypatch):
    """settings.json が壊れている場合、生のトレースバックではなく actionable な
    エラーを送出し、書き込みを拒否する（issue #28: 不正 settings.json 未処理）。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not valid json")

    from codeatrium.hooks import SettingsLoadError, install_hooks

    with pytest.raises(SettingsLoadError, match="invalid JSON"):
        install_hooks()

    # 壊れた元ファイルは無傷（書き込みを拒否した）
    assert settings_path.read_text() == "{not valid json"


def test_uninstall_hooks_malformed_json_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not valid json")

    from codeatrium.hooks import SettingsLoadError, uninstall_hooks

    with pytest.raises(SettingsLoadError, match="invalid JSON"):
        uninstall_hooks()

    assert settings_path.read_text() == "{not valid json"


def test_hook_install_cli_malformed_json_gives_actionable_message_not_traceback(
    tmp_path, monkeypatch
):
    """CLI 経由でも生トレースバックではなく actionable なメッセージで exit する。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not valid json")

    result = runner.invoke(app, ["hook", "install"])

    assert result.exit_code == 1
    assert "invalid JSON" in result.output
    assert "Traceback" not in result.output


def test_write_settings_rotates_backup_before_overwriting(tmp_path, monkeypatch):
    """2回連続の書き込みでも、直前の内容は世代アーカイブとして残る
    （issue #28: .bak 単一世代の頑健性問題）。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)

    from codeatrium.hooks import _write_settings

    _write_settings(settings_path, {"gen": 0})
    _write_settings(settings_path, {"gen": 1})
    _write_settings(settings_path, {"gen": 2})

    bak_path = settings_path.with_suffix(".json.bak")
    assert json.loads(bak_path.read_text())["gen"] == 1

    archives = sorted(settings_path.parent.glob("settings.json.bak.*"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["gen"] == 0


def test_write_settings_two_consecutive_writes_preserve_last_known_good_backup(
    tmp_path, monkeypatch
):
    """2回連続の(仮に)不正な書き込みでも、最後の正常な内容の backup が
    失われない（issue #28 のコアシナリオ: 旧実装は2回目の書き込みで .bak が
    上書きされ、最後の正常な状態への復旧手段が消えていた）。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"good": True}))

    from codeatrium.hooks import _write_settings

    _write_settings(settings_path, {"bad": 1})
    _write_settings(settings_path, {"bad": 2})

    archived_contents = [
        json.loads(p.read_text())
        for p in settings_path.parent.glob("settings.json.bak.*")
    ]
    assert {"good": True} in archived_contents


def test_write_settings_backup_rotation_caps_generations(tmp_path, monkeypatch):
    """世代アーカイブは直近 N 世代のみ保持し、無限に増え続けない。"""
    monkeypatch.setattr("codeatrium.hooks.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)

    from codeatrium.hooks import _MAX_BACKUP_GENERATIONS, _write_settings

    for gen in range(_MAX_BACKUP_GENERATIONS + 5):
        _write_settings(settings_path, {"gen": gen})

    archives = list(settings_path.parent.glob("settings.json.bak.*"))
    assert len(archives) <= _MAX_BACKUP_GENERATIONS
