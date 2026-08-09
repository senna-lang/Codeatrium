"""共有データクラス定義"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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
    git_branch: str | None = None


# ---- コードとの紐付け（ハーネス非依存の共通型。design §3.2 / §5.1） ----


@dataclass(frozen=True)
class LineRange:
    """行番号が直接分かる場合の手がかり"""

    old_start: int | None
    old_lines: int | None
    new_start: int
    new_lines: int


@dataclass(frozen=True)
class TextAnchor:
    """編集前後の文字列だけ分かる場合の手がかり。行の割り出しは core が行う"""

    old_string: str | None
    new_string: str | None


@dataclass(frozen=True)
class FileOnly:
    """ファイルしか分からない場合の手がかり"""


CodeLocator = LineRange | TextAnchor | FileOnly

# ハーネスが編集場所をどこまで正確に出せるか（design §3.3）。
# 'full' を宣言したハーネスは LineRange を出す契約を負うため、
# 解読できていない形式を安易に 'full' と申告してはならない。
EditCapability = Literal["full", "anchor", "file_only"]

TouchKind = Literal["edit", "write", "read"]


@dataclass(frozen=True)
class CodeTouch:
    """ハーネス非依存の編集記録。アダプターがこの形にして core へ渡す（design §5.1）"""

    harness: str
    tool_call_id: str
    file_path: str
    touch_kind: TouchKind
    locators: tuple[CodeLocator, ...]
    added: int
    removed: int
    ts: str | None
    # 以下は core が後から埋める
    symbol_name: str | None = None
    resolved_by: str | None = None


EdgeKind = Literal["edit", "write", "read", "mention"]
Granularity = Literal["line", "file"]


@dataclass(frozen=True)
class CodeEdge:
    """会話とコードのひも付け（design §4.1・§5.5）。

    symbol_id=None は granularity='file' を意味し、シンボルまでは特定できな
    かったが会話とファイルの関係だけは記録できた状態（design §8.1 不変条件2）。
    """

    id: str
    exchange_id: str
    file_path: str
    symbol_id: str | None
    edge_kind: EdgeKind
    granularity: Granularity
    confidence: float
    added: int
    ts: str | None
