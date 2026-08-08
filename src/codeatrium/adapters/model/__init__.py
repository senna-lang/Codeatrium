"""ModelClient registry — 蒸留 client の detect/setup/select。

core (llm.py / distiller.py) からは `resolve_client` / `check_ready` のみを使い、
どの client id がどう検出されるかの詳細はここに閉じ込める。
"""

from __future__ import annotations

from codeatrium.adapters.model.registry import (
    ClientStatus,
    discover,
    ready_clients,
    recommended_id,
    resolve_client,
    setup,
    write_client_config,
)
from codeatrium.adapters.model.types import ModelClient

__all__ = [
    "ClientStatus",
    "ModelClient",
    "discover",
    "ready_clients",
    "recommended_id",
    "resolve_client",
    "setup",
    "write_client_config",
]
