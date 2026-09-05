"""Ports implemented by harness and model adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from codeatrium.core.models import CanonicalSession, ParseResult


class LogSource(Protocol):
    """Harness transcript source; raw formats remain adapter-private."""

    id: str

    def detect(self, project_root: Path) -> bool: ...

    def list_sessions(self, project_root: Path) -> list[CanonicalSession]: ...

    def parse_exchanges(
        self, session: CanonicalSession, cursor: str | None, min_chars: int
    ) -> ParseResult: ...

    def fetch_verbatim(self, session_ref: str) -> str | None: ...


class Hooks(Protocol):
    """Optional harness lifecycle integration."""

    def install(self, project_root: Path) -> tuple[bool, str]: ...

    def uninstall(self) -> tuple[bool, str]: ...

    def fallback_recipe(self, project_root: Path) -> str: ...
