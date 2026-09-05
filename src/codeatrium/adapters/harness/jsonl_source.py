"""Reusable LogSource implementation for project-scoped JSONL harness logs."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeatrium.core.models import (
    CanonicalExchange,
    CanonicalSession,
    ExchangeArtifacts,
    FileRename,
    ParseResult,
)
from codeatrium.utils import sha256

LegacyParser = Callable[..., list]
PathResolver = Callable[[Path], Path | None]


class JsonlLogSource:
    """Adapts an existing parser without exposing its raw format to callers."""

    def __init__(
        self,
        source_id: str,
        resolve_path: PathResolver,
        parser: LegacyParser,
        pattern: str = "*.jsonl",
        touch_adapter: Any | None = None,
    ) -> None:
        self.id = source_id
        self._resolve_path = resolve_path
        self._parser = parser
        self._pattern = pattern
        self._touch_adapter = touch_adapter

    def detect(self, project_root: Path) -> bool:
        directory = self._resolve_path(project_root)
        return directory is not None and any(directory.rglob(self._pattern))

    def list_sessions(self, project_root: Path) -> list[CanonicalSession]:
        directory = self._resolve_path(project_root)
        if directory is None:
            return []
        project_key = str(project_root.resolve())
        sessions: list[CanonicalSession] = []
        for path in directory.rglob(self._pattern):
            source_session_id = str(path.resolve())
            sessions.append(
                CanonicalSession(
                    harness=self.id,
                    source_session_id=source_session_id,
                    primary_ref=source_session_id,
                    project_key=project_key,
                    started_at=datetime.fromtimestamp(
                        path.stat().st_mtime, tz=UTC
                    ).isoformat(),
                )
            )
        return sessions

    def parse_exchanges(
        self, session: CanonicalSession, cursor: str | None, min_chars: int
    ) -> ParseResult:
        last_ply_end = self._cursor_to_ply(cursor)
        legacy = self._parser(
            Path(session.primary_ref),
            min_chars=min_chars,
            last_ply_end=last_ply_end,
        )
        exchanges = tuple(
            CanonicalExchange(
                harness=self.id,
                session_ref=(
                    f"{session.primary_ref}#ply="
                    f"{exchange.ply_start}-{exchange.ply_end}"
                ),
                source_session_id=session.source_session_id,
                source_turn_id=str(exchange.ply_start),
                ply_start=exchange.ply_start,
                ply_end=exchange.ply_end,
                user_content=exchange.user_content,
                agent_content=exchange.agent_content,
                files_touched=tuple(exchange.files),
                git_branch=exchange.git_branch,
            )
            for exchange in legacy
        )
        artifacts = self._extract_artifacts(session, legacy)
        next_cursor = cursor
        if exchanges:
            next_cursor = f"v1:ply:{exchanges[-1].ply_end}"
        return ParseResult(
            exchanges=exchanges,
            next_cursor=next_cursor,
            artifacts=artifacts,
        )
    def _extract_artifacts(
        self, session: CanonicalSession, legacy: list[Any]
    ) -> tuple[ExchangeArtifacts, ...]:
        if self._touch_adapter is None:
            return ()
        try:
            raw_entries = [
                json.loads(line)
                for line in Path(session.primary_ref).read_text().splitlines()
            ]
        except (OSError, json.JSONDecodeError):
            return ()
        extract_renames = getattr(
            self._touch_adapter, "extract_file_renames", None
        )
        artifacts: list[ExchangeArtifacts] = []
        for exchange in legacy:
            entry_slice = raw_entries[exchange.ply_start : exchange.ply_end + 1]
            touches = tuple(self._touch_adapter.extract_code_touches(entry_slice))
            renames = (
                tuple(
                    FileRename(old_path, new_path, ts)
                    for old_path, new_path, ts in extract_renames(entry_slice)
                )
                if extract_renames is not None
                else ()
            )
            if touches or renames:
                artifacts.append(
                    ExchangeArtifacts(
                        source_turn_id=str(exchange.ply_start),
                        code_touches=touches,
                        file_renames=renames,
                    )
                )
        return tuple(artifacts)

    def fetch_verbatim(self, session_ref: str) -> str | None:
        path, separator, _ = session_ref.partition("#ply=")
        if not separator:
            return None
        try:
            return Path(path).read_text()
        except OSError:
            return None

    @staticmethod
    def _cursor_to_ply(cursor: str | None) -> int:
        if cursor is None:
            return -1
        prefix = "v1:ply:"
        if not cursor.startswith(prefix):
            return -1
        try:
            return int(cursor.removeprefix(prefix))
        except ValueError:
            return -1


def session_id(session: CanonicalSession) -> str:
    """Return the canonical persisted id for an adapter-owned session."""
    return sha256(f"{session.harness}:{session.source_session_id}")
