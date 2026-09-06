"""Composition helpers for the harness LogSource adapters."""

from __future__ import annotations

from codeatrium.adapters.harness import (
    claude as claude_adapter,
)
from codeatrium.adapters.harness import (
    codex as codex_adapter,
)
from codeatrium.adapters.harness import (
    grok as grok_adapter,
)
from codeatrium.adapters.harness import (
    omp_pi as omp_pi_adapter,
)
from codeatrium.adapters.harness.jsonl_source import JsonlLogSource
from codeatrium.indexer import (
    parse_codex_exchanges,
    parse_exchanges,
    parse_grok_exchanges,
    parse_omp_pi_exchanges,
)
from codeatrium.paths import (
    resolve_claude_projects_path,
    resolve_codex_sessions_path,
    resolve_grok_sessions_path,
    resolve_omp_pi_sessions_path,
)


def _resolve_codex(_project_root):
    return resolve_codex_sessions_path()


def detected_jsonl_sources() -> tuple[JsonlLogSource, ...]:
    """Return the JSONL sources supported by the current installation."""
    return (
        JsonlLogSource(
            "claude",
            resolve_claude_projects_path,
            parse_exchanges,
            touch_adapter=claude_adapter,
            parent_ref_resolver=claude_adapter.parent_session_ref,
        ),
        JsonlLogSource(
            "codex",
            _resolve_codex,
            parse_codex_exchanges,
            "rollout-*.jsonl",
            touch_adapter=codex_adapter,
        ),
        JsonlLogSource(
            "omp-pi",
            resolve_omp_pi_sessions_path,
            parse_omp_pi_exchanges,
            touch_adapter=omp_pi_adapter,
        ),
        JsonlLogSource(
            "grok",
            resolve_grok_sessions_path,
            parse_grok_exchanges,
            "updates.jsonl",
            touch_adapter=grok_adapter,
        ),
    )
