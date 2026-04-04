"""
loci status / loci hook install のテスト

status コマンド: exchange 数・蒸留済み数・DB サイズを返す
hook install  : ~/.claude/settings.json に Stop hook を登録する
"""

from __future__ import annotations

import json
from pathlib import Path

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
    assert "undistilled" in data
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
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content, distilled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ex_id2, conv_id, 2, 3, "foo bar", "baz qux", "2026-01-01T00:00:00"),
    )
    con.commit()
    con.close()

    result = runner.invoke(app, ["status", "--json"])
    data = json.loads(result.output)
    assert data["exchanges"] == 2
    assert data["distilled"] == 1
    assert data["undistilled"] == 1


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


def test_prime_outputs_instructions():
    result = runner.invoke(app, ["prime"])
    assert result.exit_code == 0
    assert "loci search" in result.output
    assert "loci context" in result.output
    assert "loci show" in result.output


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
