"""config.toml 読み込みのテスト"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from codeatrium.config import (
    DEFAULT_DISTILL_BATCH_LIMIT,
    DEFAULT_DISTILL_MIN_CHARS,
    DEFAULT_DISTILL_MODEL,
    DEFAULT_DISTILL_PROVIDER,
    DEFAULT_INDEX_MIN_CHARS,
    Config,
    load_config,
)


def test_load_config_no_file(tmp_path: Path) -> None:
    """config.toml がなければデフォルト"""
    (tmp_path / ".codeatrium").mkdir()
    cfg = load_config(tmp_path)
    assert cfg.distill_model == DEFAULT_DISTILL_MODEL
    assert cfg.distill_batch_limit == DEFAULT_DISTILL_BATCH_LIMIT
    assert cfg.index_min_chars == DEFAULT_INDEX_MIN_CHARS


def test_load_config_custom_values(tmp_path: Path) -> None:
    """カスタム値が正しく読まれる"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text(
        '[distill]\nmodel = "claude-sonnet-4-20250514"\nbatch_limit = 10\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.distill_model == "claude-sonnet-4-20250514"
    assert cfg.distill_batch_limit == 10


def test_load_config_partial(tmp_path: Path) -> None:
    """一部だけ設定した場合、残りはデフォルト"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nbatch_limit = 5\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_model == DEFAULT_DISTILL_MODEL
    assert cfg.distill_batch_limit == 5


def test_load_config_invalid_batch_limit_fallback(tmp_path: Path) -> None:
    """不正な batch_limit はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nbatch_limit = -1\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_batch_limit == DEFAULT_DISTILL_BATCH_LIMIT


def test_load_config_invalid_model_fallback(tmp_path: Path) -> None:
    """空文字のモデル名はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text('[distill]\nmodel = ""\n')
    cfg = load_config(tmp_path)
    assert cfg.distill_model == DEFAULT_DISTILL_MODEL


def test_load_config_broken_toml_fallback(tmp_path: Path) -> None:
    """壊れた TOML はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("not valid toml [[[")
    cfg = load_config(tmp_path)
    assert cfg == Config()


def test_load_config_index_min_chars(tmp_path: Path) -> None:
    """index.min_chars が正しく読まれる"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[index]\nmin_chars = 200\n")
    cfg = load_config(tmp_path)
    assert cfg.index_min_chars == 200


def test_load_config_invalid_min_chars_fallback(tmp_path: Path) -> None:
    """不正な min_chars はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[index]\nmin_chars = 0\n")
    cfg = load_config(tmp_path)
    assert cfg.index_min_chars == DEFAULT_INDEX_MIN_CHARS


def test_load_config_distill_min_chars(tmp_path: Path) -> None:
    """distill.min_chars が正しく読まれる"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nmin_chars = 200\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_min_chars == 200


def test_load_config_distill_min_chars_default(tmp_path: Path) -> None:
    """distill.min_chars 未設定時はデフォルト100"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_min_chars == DEFAULT_DISTILL_MIN_CHARS


def test_load_config_distill_min_chars_invalid_fallback(tmp_path: Path) -> None:
    """不正な distill.min_chars はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nmin_chars = -5\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_min_chars == DEFAULT_DISTILL_MIN_CHARS


def test_load_config_toml_decode_error_fallback(tmp_path: Path, capsys) -> None:
    """不正な TOML 内容（TOMLDecodeError）はデフォルトにフォールバック、警告出力"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("not valid toml [")
    cfg = load_config(tmp_path)
    assert cfg == Config()
    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_load_config_oserror_fallback(tmp_path: Path) -> None:
    """OSError（ファイルアクセスエラー）はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    config_file = codeatrium_dir / "config.toml"
    config_file.write_text("[distill]\nmodel = 'test'\n")
    # exists() は True のまま、open() だけ OSError を引き起こす
    with patch("pathlib.Path.open", side_effect=OSError("disk error")):
        cfg = load_config(tmp_path)
    assert cfg == Config()


def test_load_config_provider_default(tmp_path: Path) -> None:
    """[distill] provider キーがないときはデフォルト"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_provider == DEFAULT_DISTILL_PROVIDER
    assert cfg.distill_base_url is None


def test_load_config_provider_invalid_fallback(tmp_path: Path) -> None:
    """不正な provider はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nprovider = 'badprovider'\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_provider == DEFAULT_DISTILL_PROVIDER


def test_load_config_provider_openai_with_base_url(tmp_path: Path) -> None:
    """provider = 'openai' で base_url が設定されている場合"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text(
        "[distill]\nprovider = 'openai'\nbase_url = 'http://localhost:11434/v1'\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.distill_provider == "openai"
    assert cfg.distill_base_url == "http://localhost:11434/v1"


def test_load_config_provider_openai_missing_base_url_fallback(tmp_path: Path) -> None:
    """provider = 'openai' だが base_url がない場合はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nprovider = 'openai'\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_provider == DEFAULT_DISTILL_PROVIDER
    assert cfg.distill_base_url is None


def test_load_config_provider_openai_empty_base_url_fallback(tmp_path: Path) -> None:
    """provider = 'openai' で base_url が空文字列の場合はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text(
        "[distill]\nprovider = 'openai'\nbase_url = ''\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.distill_provider == DEFAULT_DISTILL_PROVIDER


def test_load_config_provider_claude_no_base_url(tmp_path: Path) -> None:
    """provider = 'claude' の場合は base_url は不要"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text("[distill]\nprovider = 'claude'\n")
    cfg = load_config(tmp_path)
    assert cfg.distill_provider == "claude"
    assert cfg.distill_base_url is None
