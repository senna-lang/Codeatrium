"""共有データクラス定義"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PalaceObject:
    """蒸留済み palace object"""

    exchange_core: str
    specific_context: str
    room_assignments: list[dict[str, Any]]
    files_touched: list[str] = field(default_factory=list)


@dataclass
class BM25Result:
    """BM25 verbatim 検索結果"""

    exchange_id: str
    user_content: str
    agent_content: str
    bm25_score: float


@dataclass
class HNSWPalaceResult:
    """HNSW distilled 検索結果"""

    exchange_id: str
    user_content: str
    agent_content: str
    exchange_core: str
    specific_context: str
    distance: float


@dataclass
class FusedResult:
    """RRF 融合検索結果（SPEC 準拠の出力フォーマット）"""

    exchange_id: str
    user_content: str
    agent_content: str
    score: float
    exchange_core: str | None = None
    specific_context: str | None = None
    verbatim_ref: str | None = None
    rooms: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
