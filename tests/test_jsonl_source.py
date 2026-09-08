"""
JsonlLogSource の `.codeatrium/ignore` プライバシフィルタと JSONL artifact 抽出のテスト。

artifact は indexer と同じ「有効な JSON エントリのみ」の ply 座標を使い、空行または
破損行を含むログでも対応する exchange へ touch を関連付ける。
"""
import json
from pathlib import Path

import pytest

from codeatrium.adapters.harness.jsonl_source import JsonlLogSource
from codeatrium.core.models import CanonicalSession
from codeatrium.indexer import Exchange
from codeatrium.models import CodeTouch


def _make_session(project_root: Path) -> CanonicalSession:
    return CanonicalSession(
        harness="fake",
        source_session_id="session-1",
        primary_ref=str(project_root / "session.jsonl"),
        project_key=str(project_root),
    )


class _RecordingTouchAdapter:
    """渡された raw entry から、どの entry が artifact 化されたかを返すテスト用 adapter。"""

    def extract_code_touches(
        self, entries: list[dict[str, object] | None]
    ) -> list[CodeTouch]:
        return [
            CodeTouch(
                harness="fake",
                tool_call_id=str(entry["id"]),
                file_path=str(entry["path"]),
                touch_kind="edit",
                locators=(),
                added=0,
                removed=0,
                ts=None,
            )
            for entry in entries
            if entry is not None
        ]


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


@pytest.mark.parametrize("invalid_line", ["", '{"broken":'])
def test_parse_exchanges_artifacts_skip_blank_and_malformed_lines(
    tmp_path: Path, invalid_line: str
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    session = _make_session(project_root)
    Path(session.primary_ref).write_text(
        "\n".join(
            [
                json.dumps({"id": "earlier", "path": "src/earlier.py"}),
                invalid_line,
                json.dumps({"id": "target", "path": "src/target.py"}),
            ]
        ),
        encoding="utf-8",
    )

    def fake_parser(jsonl_path: Path, min_chars: int, last_ply_end: int) -> list[Exchange]:
        return [
            Exchange(
                id="ex-target",
                conversation_id="conv1",
                ply_start=1,
                ply_end=1,
                user_content="target request",
                agent_content="target reply",
                files=[],
            )
        ]

    source = JsonlLogSource(
        "fake",
        lambda root: root,
        fake_parser,
        touch_adapter=_RecordingTouchAdapter(),
    )

    result = source.parse_exchanges(session, cursor=None, min_chars=1)

    assert [
        (artifact.source_turn_id, [touch.file_path for touch in artifact.code_touches])
        for artifact in result.artifacts
    ] == [("1", ["src/target.py"])]
