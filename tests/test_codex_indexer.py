"""Codex rollout JSONL を exchange・code edge・改名記録へ取り込む契約を検証する。"""

from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_file, parse_codex_exchanges

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "codex.jsonl"


def _write_rollout(path: Path, project_root: Path) -> None:
    """合成ログの /repo を一時プロジェクトの絶対パスへ置き換えて保存する。"""
    path.write_text(_FIXTURE.read_text().replace('"/repo', f'"{project_root}'))


def test_parse_codex_exchanges_groups_turn_content_and_touched_files(
    tmp_path: Path,
) -> None:
    """ユーザー入力から次の入力直前までを一つの Codex exchange にする。"""
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, tmp_path)

    exchanges = parse_codex_exchanges(rollout, min_chars=1)

    assert len(exchanges) == 1
    assert exchanges[0].user_content == "fs.list_dir を Result 型にして"
    assert exchanges[0].agent_content == "list_dir を編集します。"
    assert exchanges[0].git_branch == "main"
    assert exchanges[0].files == [
        str(tmp_path / "src" / "fs.py"),
        str(tmp_path / "src" / "result.py"),
        str(tmp_path / "src" / "legacy.py"),
        str(tmp_path / "src" / "old_name.py"),
    ]


def test_index_file_indexes_codex_edges_and_move_paths(tmp_path: Path) -> None:
    """Codex の編集記録と move_path を同じ transaction で永続化する。"""
    project_root = tmp_path / "project"
    source_dir = project_root / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")
    (source_dir / "new_name.py").write_text("x = 2\n")

    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, project_root)
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_file(
        rollout,
        db_path,
        min_chars=1,
        project_root=project_root,
        harness="codex",
    )

    assert indexed == 1
    con = get_connection(db_path)
    assert con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 1
    assert (
        con.execute(
            "SELECT COUNT(*) FROM code_touches WHERE harness = 'codex'"
        ).fetchone()[0]
        >= 4
    )
    assert con.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0] >= 4
    rename_rows = con.execute(
        "SELECT old_path, new_path, source FROM file_renames"
    ).fetchall()
    assert [tuple(row) for row in rename_rows] == [
        ("src/old_name.py", "src/new_name.py", "harness")
    ]
    con.close()


def test_index_file_codex_is_incremental(tmp_path: Path) -> None:
    """同じ Codex rollout を再実行しても exchange を重複登録しない。"""
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, tmp_path)
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    assert index_file(rollout, db_path, min_chars=1, harness="codex") == 1
    assert index_file(rollout, db_path, min_chars=1, harness="codex") == 0
