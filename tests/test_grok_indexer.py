"""grok の ACP セッションログを exchange・code edge へ取り込む契約を検証する。"""

from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_file, parse_grok_exchanges

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "grok.jsonl"


def _write_session(path: Path, project_root: Path) -> None:
    path.write_text(_FIXTURE.read_text().replace("/repo/", f"{project_root}/"))


def test_parse_grok_exchanges_uses_user_message_chunk_as_boundary(
    tmp_path: Path,
) -> None:
    """agent_thought_chunk は思考なので agent_content に含めない。"""
    session = tmp_path / "updates.jsonl"
    _write_session(session, tmp_path)

    exchanges = parse_grok_exchanges(session, min_chars=1)

    assert len(exchanges) == 1
    assert exchanges[0].user_content == "list_dir を Result 型にして"
    assert exchanges[0].agent_content == "list_dir を Result 型に変更しました。"
    assert "Result 型に変える必要がある" not in exchanges[0].agent_content


def test_index_file_records_grok_touches_as_file_granularity(tmp_path: Path) -> None:
    """anchor capability なので行粒度には上がらず、ファイル粒度で必ず1本張る（不変条件2）。"""
    project_root = tmp_path / "project"
    source_dir = project_root / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")

    session = tmp_path / "updates.jsonl"
    _write_session(session, project_root)
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_file(
        session, db_path, min_chars=1, project_root=project_root, harness="grok"
    )

    assert indexed == 1
    con = get_connection(db_path)
    touched = {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT file_path FROM code_touches WHERE harness = 'grok'"
        )
    }
    assert touched == {"src/fs.py", "src/result.py"}
    # 失敗した search_replace のファイルは記録されない
    assert "src/missing.py" not in touched

    kinds = {row[0] for row in con.execute("SELECT DISTINCT locator_kind FROM code_touches")}
    assert kinds == {"anchor"}
    granularities = {
        row[0] for row in con.execute("SELECT DISTINCT granularity FROM code_edges")
    }
    assert granularities == {"file"}
    con.close()


def test_index_file_grok_is_incremental(tmp_path: Path) -> None:
    """同じセッションを再実行しても exchange を重複登録しない。"""
    session = tmp_path / "updates.jsonl"
    _write_session(session, tmp_path)
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    assert index_file(session, db_path, min_chars=1, harness="grok") == 1
    assert index_file(session, db_path, min_chars=1, harness="grok") == 0
