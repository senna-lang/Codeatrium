"""codeatrium.adapters.model.registry のユニットテスト"""

from __future__ import annotations

from unittest.mock import MagicMock

from codeatrium.adapters.model.registry import (
    check_ready,
    detect_claude_cli,
    detect_ollama_ft,
    discover,
    ready_clients,
    recommended_id,
    resolve_client,
    setup,
    write_client_config,
)
from codeatrium.adapters.model.types import ClientStatus
from codeatrium.config import LOCAL_DISTILL_BASE_URL, LOCAL_DISTILL_MODEL

# ---- detect_ollama_ft ----


def test_detect_ollama_ft_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    status = detect_ollama_ft()
    assert status.state == "unavailable"
    assert status.client is None


def test_detect_ollama_ft_model_not_pulled(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stdout="NAME\nother-model:latest\n"),
    )
    status = detect_ollama_ft()
    assert status.state == "setupable"
    assert status.client is None


def test_detect_ollama_ft_ready(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stdout=f"NAME\n{LOCAL_DISTILL_MODEL}\n"),
    )
    status = detect_ollama_ft()
    assert status.state == "ready"
    assert status.client is not None
    assert status.client.id == "ollama-ft"
    assert status.client.base_url == LOCAL_DISTILL_BASE_URL


def test_detect_ollama_ft_list_command_fails(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: MagicMock(returncode=1, stdout="")
    )
    status = detect_ollama_ft()
    assert status.state == "setupable"


# ---- detect_claude_cli ----


def test_detect_claude_cli_missing(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    status = detect_claude_cli()
    assert status.state == "unavailable"
    assert status.client is None


def test_detect_claude_cli_ready(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude")
    status = detect_claude_cli()
    assert status.state == "ready"
    assert status.client is not None
    assert status.client.id == "claude-cli"
    assert status.client.provider == "claude"


# ---- discover / ready_clients / recommended_id ----


def test_discover_returns_ollama_then_claude_order(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    statuses = discover()
    assert [s.id for s in statuses] == ["ollama-ft", "claude-cli"]


def test_ready_clients_filters_by_state() -> None:
    statuses = [
        ClientStatus(id="a", label="A", state="ready", reason="ready"),
        ClientStatus(id="b", label="B", state="unavailable", reason="no"),
        ClientStatus(id="c", label="C", state="setupable", reason="setup"),
    ]
    assert [s.id for s in ready_clients(statuses)] == ["a"]


def test_recommended_id_prefers_ollama_ft() -> None:
    statuses = [
        ClientStatus(id="claude-cli", label="Claude CLI", state="ready", reason="ready"),
        ClientStatus(id="ollama-ft", label="Ollama", state="ready", reason="ready"),
    ]
    assert recommended_id(statuses) == "ollama-ft"


def test_recommended_id_falls_back_to_first_ready_when_ollama_not_ready() -> None:
    statuses = [
        ClientStatus(id="claude-cli", label="Claude CLI", state="ready", reason="ready"),
    ]
    assert recommended_id(statuses) == "claude-cli"


def test_recommended_id_none_when_no_ready() -> None:
    statuses = [ClientStatus(id="a", label="A", state="unavailable", reason="no")]
    assert recommended_id(statuses) is None


# ---- setup ----


def test_setup_ollama_ft_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    ok, msg = setup("ollama-ft")
    assert ok is False
    assert "ollama binary not found" in msg


def test_setup_ollama_ft_pull_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: MagicMock(returncode=0))
    ok, msg = setup("ollama-ft")
    assert ok is True
    assert LOCAL_DISTILL_MODEL in msg


def test_setup_ollama_ft_pull_fails(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: MagicMock(returncode=1, stderr="boom")
    )
    ok, msg = setup("ollama-ft")
    assert ok is False
    assert "failed" in msg


def test_setup_unsupported_client_id() -> None:
    ok, msg = setup("claude-cli")
    assert ok is False
    assert "no automated setup" in msg


# ---- resolve_client ----


def test_resolve_client_claude_cli(monkeypatch) -> None:
    from codeatrium.config import Config

    cfg = Config(distill_model="claude-haiku-4-5-20251001")
    client = resolve_client("claude-cli", cfg)
    assert client.provider == "claude"
    assert client.base_url is None


def test_resolve_client_ollama_ft_uses_config_overrides() -> None:
    from codeatrium.config import Config

    cfg = Config(distill_model="custom-ft-model", distill_base_url="http://x:1/v1")
    client = resolve_client("ollama-ft", cfg)
    assert client.model == "custom-ft-model"
    assert client.base_url == "http://x:1/v1"


def test_resolve_client_openai_compat_requires_base_url() -> None:
    from codeatrium.config import Config

    cfg = Config(distill_model="m", distill_base_url=None)
    try:
        resolve_client("openai-compat", cfg)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_resolve_client_unknown_id_raises() -> None:
    from codeatrium.config import Config

    try:
        resolve_client("bogus", Config())
        raised = False
    except ValueError:
        raised = True
    assert raised


# ---- check_ready ----


def test_check_ready_dispatches_to_detector(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    status = check_ready("claude-cli")
    assert status.state == "unavailable"


def test_check_ready_unknown_client_is_unavailable() -> None:
    status = check_ready("openai-compat")
    assert status.state == "unavailable"


# ---- write_client_config ----


def test_write_client_config_writes_client_model_base_url(tmp_path) -> None:
    from codeatrium.adapters.model.types import ModelClient

    config_path = tmp_path / "config.toml"
    client = ModelClient(
        id="ollama-ft",
        provider="openai",
        model=LOCAL_DISTILL_MODEL,
        base_url=LOCAL_DISTILL_BASE_URL,
        label="Ollama (local FT model)",
    )
    write_client_config(config_path, client)
    content = config_path.read_text()
    assert 'client = "ollama-ft"' in content
    assert f'model = "{LOCAL_DISTILL_MODEL}"' in content
    assert f'base_url = "{LOCAL_DISTILL_BASE_URL}"' in content
    assert "provider" not in content


def test_write_client_config_drops_legacy_provider_key(tmp_path) -> None:
    from codeatrium.adapters.model.types import ModelClient

    config_path = tmp_path / "config.toml"
    config_path.write_text('[distill]\nprovider = "claude"\nbatch_limit = 5\n')
    client = ModelClient(
        id="claude-cli",
        provider="claude",
        model="claude-haiku-4-5-20251001",
        base_url=None,
        label="Claude CLI",
    )
    write_client_config(config_path, client)
    content = config_path.read_text()
    assert 'client = "claude-cli"' in content
    assert 'provider = "claude"' not in content
    assert "batch_limit = 5" in content
    assert "base_url" not in content


def test_write_client_config_preserves_index_min_chars(tmp_path) -> None:
    from codeatrium.adapters.model.types import ModelClient

    config_path = tmp_path / "config.toml"
    config_path.write_text('[distill]\nprovider = "claude"\n\n[index]\nmin_chars = 200\n')
    client = ModelClient(
        id="claude-cli",
        provider="claude",
        model="claude-haiku-4-5-20251001",
        base_url=None,
        label="Claude CLI",
    )
    write_client_config(config_path, client)
    content = config_path.read_text()
    assert "min_chars = 200" in content
