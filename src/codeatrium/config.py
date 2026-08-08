"""設定ファイルの読み込み — .codeatrium/config.toml

Supports distill_provider (claude/openai) and distill_base_url for provider switching."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.toml"

# ---- デフォルト値 ----

DEFAULT_DISTILL_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_DISTILL_BATCH_LIMIT = 20
DEFAULT_INDEX_MIN_CHARS = 50
DEFAULT_DISTILL_MIN_CHARS = 100
DEFAULT_DISTILL_PROVIDER = "claude"
VALID_DISTILL_PROVIDERS = frozenset({"claude", "openai"})
VALID_DISTILL_CLIENT_IDS = frozenset({"ollama-ft", "claude-cli", "openai-compat"})

# ローカル蒸留モデル（`loci init` の対話プロンプトで opt-in した場合のデフォルト値）。
# GGUF は Ollama 経由で hf.co/<repo>:<quant> の形式で直接 pull できる。
LOCAL_DISTILL_MODEL = "hf.co/sennaLLMLearner/qwen2.5-7b-memory-distiller:Q4_K_M"
LOCAL_DISTILL_BASE_URL = "http://localhost:11434/v1"


@dataclass
class Config:
    """ユーザー設定"""

    distill_model: str = DEFAULT_DISTILL_MODEL
    distill_batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT
    index_min_chars: int = DEFAULT_INDEX_MIN_CHARS
    distill_min_chars: int = DEFAULT_DISTILL_MIN_CHARS
    distill_provider: str = "claude"
    distill_base_url: str | None = None
    distill_client: str | None = None
    distill_unconfigured: bool = True


def load_config(project_root: Path) -> Config:
    """project_root/.codeatrium/config.toml を読んで Config を返す。
    ファイルがなければデフォルト。不正な値は警告してデフォルトにフォールバック。
    """
    config_path = project_root / ".codeatrium" / CONFIG_FILENAME
    if not config_path.exists():
        return Config()

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError) as e:
        print(f"Warning: failed to parse {config_path}: {e}", file=sys.stderr)
        return Config()

    distill: dict[str, Any] = data.get("distill", {})

    model = distill.get("model", DEFAULT_DISTILL_MODEL)
    if not isinstance(model, str) or not model.strip():
        print(
            "Warning: distill.model must be a non-empty string, using default.",
            file=sys.stderr,
        )
        model = DEFAULT_DISTILL_MODEL

    batch_limit = distill.get("batch_limit", DEFAULT_DISTILL_BATCH_LIMIT)
    if not isinstance(batch_limit, int) or batch_limit < 1:
        print(
            "Warning: distill.batch_limit must be a positive integer, using default.",
            file=sys.stderr,
        )
        batch_limit = DEFAULT_DISTILL_BATCH_LIMIT

    index: dict[str, Any] = data.get("index", {})

    min_chars = index.get("min_chars", DEFAULT_INDEX_MIN_CHARS)
    if not isinstance(min_chars, int) or min_chars < 1:
        print(
            "Warning: index.min_chars must be a positive integer, using default.",
            file=sys.stderr,
        )
        min_chars = DEFAULT_INDEX_MIN_CHARS

    distill_min_chars = distill.get("min_chars", DEFAULT_DISTILL_MIN_CHARS)
    if not isinstance(distill_min_chars, int) or distill_min_chars < 1:
        print(
            "Warning: distill.min_chars must be a positive integer, using default.",
            file=sys.stderr,
        )
        distill_min_chars = DEFAULT_DISTILL_MIN_CHARS

    provider = distill.get("provider", DEFAULT_DISTILL_PROVIDER)
    if not isinstance(provider, str) or provider not in VALID_DISTILL_PROVIDERS:
        print(
            "Warning: distill.provider must be one of {claude, openai}, using default.",
            file=sys.stderr,
        )
        provider = DEFAULT_DISTILL_PROVIDER

    base_url = distill.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        base_url = None

    if provider == "openai" and (base_url is None or not base_url.strip()):
        print(
            "Warning: distill.provider is 'openai' but base_url is not set, falling back to 'claude'.",
            file=sys.stderr,
        )
        provider = DEFAULT_DISTILL_PROVIDER
        base_url = None

    raw_client = distill.get("client")
    provider_specified = "provider" in distill

    distill_client: str | None
    distill_unconfigured: bool
    if isinstance(raw_client, str) and raw_client in VALID_DISTILL_CLIENT_IDS:
        distill_client = raw_client
        distill_unconfigured = False
    elif isinstance(raw_client, str):
        print(
            f"Warning: unknown distill.client '{raw_client}', "
            "treating distill as unconfigured.",
            file=sys.stderr,
        )
        distill_client = None
        distill_unconfigured = True
    elif provider_specified:
        # provider/base_url はここまでの検証で安全な値に確定済み（例: openai だが
        # base_url 欠落は "claude" にフォールバック済み）。それを踏まえて解決する。
        distill_unconfigured = False
        if provider == "claude":
            distill_client = "claude-cli"
        else:
            # provider = "openai": Ollama のデフォルトポート(11434)を使っていれば
            # ローカル FT (ollama-ft) とみなし、それ以外は汎用 openai-compat とする。
            is_ollama = bool(base_url) and "11434" in base_url
            distill_client = "ollama-ft" if is_ollama else "openai-compat"
    else:
        distill_client = None
        distill_unconfigured = True

    return Config(
        distill_model=model,
        distill_batch_limit=batch_limit,
        index_min_chars=min_chars,
        distill_min_chars=distill_min_chars,
        distill_provider=provider,
        distill_base_url=base_url,
        distill_client=distill_client,
        distill_unconfigured=distill_unconfigured,
    )
