"""設定ファイルの読み込み — .codeatrium/config.toml"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.toml"

# ---- デフォルト値 ----

DEFAULT_DISTILL_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_DISTILL_BATCH_LIMIT = 20


@dataclass
class Config:
    """ユーザー設定"""

    distill_model: str = DEFAULT_DISTILL_MODEL
    distill_batch_limit: int = DEFAULT_DISTILL_BATCH_LIMIT


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
    except Exception as e:
        import sys

        print(f"Warning: failed to parse {config_path}: {e}", file=sys.stderr)
        return Config()

    distill: dict[str, Any] = data.get("distill", {})

    model = distill.get("model", DEFAULT_DISTILL_MODEL)
    if not isinstance(model, str) or not model.strip():
        import sys

        print(
            "Warning: distill.model must be a non-empty string, using default.",
            file=sys.stderr,
        )
        model = DEFAULT_DISTILL_MODEL

    batch_limit = distill.get("batch_limit", DEFAULT_DISTILL_BATCH_LIMIT)
    if not isinstance(batch_limit, int) or batch_limit < 1:
        import sys

        print(
            "Warning: distill.batch_limit must be a positive integer, using default.",
            file=sys.stderr,
        )
        batch_limit = DEFAULT_DISTILL_BATCH_LIMIT

    return Config(
        distill_model=model,
        distill_batch_limit=batch_limit,
    )
