"""loci distill コマンドの client 解決フローのテスト

unconfigured / not-ready 時の TTY 再選択・非対話 skip・silent fallback 禁止・
`--setup` の config 保存を検証する。distill_all 自体は distiller_all をモックして
「どの backend で呼ばれたか」だけを見る。
"""

from __future__ import annotations

from typer.testing import CliRunner

from codeatrium.adapters.model.types import ClientStatus, ModelClient
from codeatrium.cli import app
from codeatrium.db import init_db

runner = CliRunner()

_CLAUDE_CLIENT = ModelClient(
    id="claude-cli",
    provider="claude",
    model="claude-haiku-4-5-20251001",
    base_url=None,
    label="Claude CLI",
)
_OLLAMA_CLIENT = ModelClient(
    id="ollama-ft",
    provider="openai",
    model="ft-model",
    base_url="http://localhost:11434/v1",
    label="Ollama (local FT model)",
)


def _init_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    init_db(codeatrium_dir / "memory.db")
    return codeatrium_dir


def _write_config(codeatrium_dir, body: str) -> None:
    (codeatrium_dir / "config.toml").write_text(body)


def _stub_distill_all(monkeypatch, calls: list):
    def _fake(db, **kwargs):
        calls.append(kwargs.get("backend"))
        return (0, 0)

    monkeypatch.setattr("codeatrium.distiller.distill_all", _fake)


# ---- unconfigured ----


def test_distill_unconfigured_non_tty_skips_without_error(tmp_path, monkeypatch) -> None:
    """unconfigured かつ非対話なら暗黙 client を作らず warn+exit0 する"""
    _init_project(tmp_path, monkeypatch)
    calls: list = []
    _stub_distill_all(monkeypatch, calls)
    monkeypatch.setattr("codeatrium.cli.distill_cmd._is_interactive", lambda: False)

    result = runner.invoke(app, ["distill"])

    assert result.exit_code == 0
    assert "not configured" in result.output
    assert calls == []


def test_distill_unconfigured_tty_prompts_and_uses_selection_once(
    tmp_path, monkeypatch
) -> None:
    """unconfigured かつ対話なら Ready 一覧から再選択できる（config は保存しない）"""
    codeatrium_dir = _init_project(tmp_path, monkeypatch)
    calls: list = []
    _stub_distill_all(monkeypatch, calls)
    monkeypatch.setattr("codeatrium.cli.distill_cmd._is_interactive", lambda: True)
    monkeypatch.setattr(
        "codeatrium.adapters.model.registry.discover",
        lambda: [
            ClientStatus(
                id="claude-cli",
                label="Claude CLI",
                state="ready",
                reason="ready",
                client=_CLAUDE_CLIENT,
            )
        ],
    )

    result = runner.invoke(app, ["distill"], input="1\n")

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0].provider == "claude"
    # once のみ: config は書き換わらない
    assert not (codeatrium_dir / "config.toml").exists()


# ---- configured but not ready ----


def test_distill_configured_not_ready_non_tty_does_not_autoswitch(
    tmp_path, monkeypatch
) -> None:
    """configured client が Ready でないとき、非対話では別 client に自動切替しない"""
    codeatrium_dir = _init_project(tmp_path, monkeypatch)
    _write_config(codeatrium_dir, '[distill]\nclient = "ollama-ft"\n')
    calls: list = []
    _stub_distill_all(monkeypatch, calls)
    monkeypatch.setattr("codeatrium.cli.distill_cmd._is_interactive", lambda: False)
    monkeypatch.setattr(
        "codeatrium.adapters.model.registry.check_ready",
        lambda client_id: ClientStatus(
            id="ollama-ft",
            label="Ollama (local FT model)",
            state="unavailable",
            reason="ollama binary not found in PATH",
        ),
    )

    result = runner.invoke(app, ["distill"])

    assert result.exit_code == 0
    assert "Not switching automatically" in result.output
    assert calls == []


def test_distill_configured_ready_uses_it_without_prompting(tmp_path, monkeypatch) -> None:
    """configured client が Ready ならそのまま使い、discover/prompt は起きない"""
    codeatrium_dir = _init_project(tmp_path, monkeypatch)
    _write_config(
        codeatrium_dir,
        '[distill]\nclient = "claude-cli"\nmodel = "claude-haiku-4-5-20251001"\n',
    )
    calls: list = []
    _stub_distill_all(monkeypatch, calls)
    monkeypatch.setattr("codeatrium.cli.distill_cmd._is_interactive", lambda: False)
    monkeypatch.setattr(
        "codeatrium.adapters.model.registry.check_ready",
        lambda client_id: ClientStatus(
            id="claude-cli",
            label="Claude CLI",
            state="ready",
            reason="ready",
            client=_CLAUDE_CLIENT,
        ),
    )

    result = runner.invoke(app, ["distill"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0].provider == "claude"


# ---- --setup ----


def test_distill_setup_saves_selection_to_config(tmp_path, monkeypatch) -> None:
    """`loci distill --setup` は選んだ client を config.toml に保存する"""
    codeatrium_dir = _init_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "codeatrium.adapters.model.registry.discover",
        lambda: [
            ClientStatus(
                id="ollama-ft",
                label="Ollama (local FT model)",
                state="ready",
                reason="ready",
                client=_OLLAMA_CLIENT,
            ),
            ClientStatus(
                id="claude-cli",
                label="Claude CLI",
                state="ready",
                reason="ready",
                client=_CLAUDE_CLIENT,
            ),
        ],
    )

    result = runner.invoke(app, ["distill", "--setup"], input="2\n")

    assert result.exit_code == 0
    config = (codeatrium_dir / "config.toml").read_text()
    assert 'client = "claude-cli"' in config


def test_distill_setup_no_ready_client_exits_nonzero(tmp_path, monkeypatch) -> None:
    """--setup 時に Ready client が無ければエラー終了する"""
    codeatrium_dir = _init_project(tmp_path, monkeypatch)
    monkeypatch.setattr("codeatrium.adapters.model.registry.discover", lambda: [])

    result = runner.invoke(app, ["distill", "--setup"])

    assert result.exit_code != 0
    assert not (codeatrium_dir / "config.toml").exists()


# ---- not initialized ----


def test_distill_not_initialized_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["distill"])
    assert result.exit_code == 1
    assert "loci init" in result.output
