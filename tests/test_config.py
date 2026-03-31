"""config.toml 読み込みのテスト"""

from __future__ import annotations

from pathlib import Path

from codeatrium.config import (
    DEFAULT_DISTILL_BATCH_LIMIT,
    DEFAULT_DISTILL_MODEL,
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
    (codeatrium_dir / "config.toml").write_text('[distill]\nbatch_limit = 5\n')
    cfg = load_config(tmp_path)
    assert cfg.distill_model == DEFAULT_DISTILL_MODEL
    assert cfg.distill_batch_limit == 5


def test_load_config_invalid_batch_limit_fallback(tmp_path: Path) -> None:
    """不正な batch_limit はデフォルトにフォールバック"""
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    (codeatrium_dir / "config.toml").write_text('[distill]\nbatch_limit = -1\n')
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
