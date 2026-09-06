"""Canonical records exchanged between harness adapters and the memory core."""

from __future__ import annotations

from dataclasses import dataclass, field

from codeatrium.models import CodeTouch


@dataclass(frozen=True)
class CanonicalSession:
    """A harness-private session made stable for project-local persistence."""

    harness: str
    source_session_id: str
    primary_ref: str
    project_key: str
    started_at: str | None = None
    title: str | None = None
    git_branch_last: str | None = None
    parent_session_ref: str | None = None


@dataclass(frozen=True)
class CanonicalExchange:
    """A substantive human turn and the agent work until the next one."""

    harness: str
    session_ref: str
    source_session_id: str
    source_turn_id: str
    ply_start: int
    ply_end: int
    user_content: str
    agent_content: str
    files_touched: tuple[str, ...] = field(default_factory=tuple)
    git_branch: str | None = None
    agent_model: str | None = None
    agent_provider: str | None = None

    def __post_init__(self) -> None:
        if not self.harness or not self.session_ref:
            raise ValueError("harness and session_ref are required")
        if not self.source_session_id or not self.source_turn_id:
            message = "source session and turn identifiers are required"
            raise ValueError(message)
        if not self.user_content.strip():
            raise ValueError("user_content must not be empty")
        if self.ply_start > self.ply_end:
            raise ValueError("ply_start must not exceed ply_end")



@dataclass(frozen=True)
class FileRename:
    """A harness-reported move associated with one canonical exchange."""

    old_path: str
    new_path: str
    ts: str | None = None


@dataclass(frozen=True)
class ExchangeArtifacts:
    """Adapter-normalized edits and moves associated with one source turn."""

    source_turn_id: str
    code_touches: tuple[CodeTouch, ...] = field(default_factory=tuple)
    file_renames: tuple[FileRename, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.source_turn_id:
            raise ValueError("source_turn_id is required")
@dataclass(frozen=True)
class ParseResult:
    """Adapter output plus its opaque incremental resume token."""
    exchanges: tuple[CanonicalExchange, ...]
    next_cursor: str | None
    artifacts: tuple[ExchangeArtifacts, ...] = field(default_factory=tuple)
    exhausted: bool = True
    rescan: bool = False

    def __post_init__(self) -> None:
        turn_ids = {exchange.source_turn_id for exchange in self.exchanges}
        artifact_turn_ids = {artifact.source_turn_id for artifact in self.artifacts}
        if not artifact_turn_ids.issubset(turn_ids):
            raise ValueError("artifacts must belong to returned exchanges")
