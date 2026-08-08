"""ModelClient registry の値型。

ModelClient: 蒸留実行に必要な transport パラメータ（llm.DistillBackend に渡す元）。
ClientStatus: discover() の1件分。state は "ready" | "setupable" | "unavailable"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ClientState = Literal["ready", "setupable", "unavailable"]


@dataclass(frozen=True)
class ModelClient:
    """蒸留実行の transport パラメータ。provider は llm.DistillBackend の provider に対応。"""

    id: str
    provider: Literal["claude", "openai"]
    model: str
    base_url: str | None
    label: str


@dataclass(frozen=True)
class ClientStatus:
    """discover() が返す1 client の状態。

    state="ready" のときのみ client が非 None。
    state="setupable" のときは setup_hint に案内文、setup() 呼び出しで Ready 化を試みられる。
    """

    id: str
    label: str
    state: ClientState
    reason: str
    client: ModelClient | None = None
