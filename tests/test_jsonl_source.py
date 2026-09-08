"""
JsonlLogSource.parse_exchanges の `.codeatrium/ignore` プライバシフィルタのテスト
（issue #36: list_sessions/parse_exchanges 経由でも同じ保護が効くことを保証する）
"""

from pathlib import Path

from codeatrium.adapters.harness.jsonl_source import JsonlLogSource
from codeatrium.core.models import CanonicalSession
from codeatrium.indexer import Exchange


def _make_session(project_root: Path) -> CanonicalSession:
    return CanonicalSession(
        harness="fake",
        source_session_id="session-1",
        primary_ref=str(project_root / "session.jsonl"),
        project_key=str(project_root),
    )


def test_parse_exchanges_excludes_exchange_touching_ignored_file(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    codeatrium_dir = project_root / ".codeatrium"
    codeatrium_dir.mkdir(parents=True)
    (codeatrium_dir / "ignore").write_text("secrets/*\n")

    def fake_parser(jsonl_path: Path, min_chars: int, last_ply_end: int) -> list[Exchange]:
        return [
            Exchange(
                id="ex1",
                conversation_id="conv1",
                ply_start=0,
                ply_end=1,
                user_content="normal request",
                agent_content="normal reply",
                files=["src/auth.py"],
            ),
            Exchange(
                id="ex2",
                conversation_id="conv1",
                ply_start=2,
                ply_end=3,
                user_content="secret request",
                agent_content="secret reply",
                files=["secrets/api_key.txt"],
            ),
        ]

    source = JsonlLogSource("fake", lambda root: root, fake_parser)
    session = _make_session(project_root)

    result = source.parse_exchanges(session, cursor=None, min_chars=1)

    assert [ex.source_turn_id for ex in result.exchanges] == ["0"]
    assert result.exchanges[0].user_content == "normal request"


def test_parse_exchanges_without_ignore_file_keeps_everything(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()

    def fake_parser(jsonl_path: Path, min_chars: int, last_ply_end: int) -> list[Exchange]:
        return [
            Exchange(
                id="ex1",
                conversation_id="conv1",
                ply_start=0,
                ply_end=1,
                user_content="secret request",
                agent_content="secret reply",
                files=["secrets/api_key.txt"],
            ),
        ]

    source = JsonlLogSource("fake", lambda root: root, fake_parser)
    session = _make_session(project_root)

    result = source.parse_exchanges(session, cursor=None, min_chars=1)

    assert len(result.exchanges) == 1
