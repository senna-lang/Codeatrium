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
