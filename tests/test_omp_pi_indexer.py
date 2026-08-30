"""omp-pi のセッション JSONL を exchange・code edge・改名記録へ取り込む契約を検証する。"""

from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_file, parse_omp_pi_exchanges

_FIXTURE = Path(__file__).parent / "fixtures" / "harness_logs" / "omp_pi.jsonl"


def _write_session(path: Path, project_root: Path) -> None:
    """合成ログの /repo を一時プロジェクトの絶対パスへ置き換えて保存する。

    ヘッダ内のパスは相対のまま残る（実ログと同じ条件を保つため）。置換されるのは
    session.cwd と、もともと絶対で書かれている箇所だけ。
    """
    path.write_text(_FIXTURE.read_text().replace('"/repo', f'"{project_root}'))


def test_parse_omp_pi_exchanges_uses_user_messages_as_boundaries(
    tmp_path: Path,
) -> None:
    """toolResult / developer / custom は境界にせず、text ブロックだけを本文に採る。"""
    session = tmp_path / "session.jsonl"
    _write_session(session, tmp_path)

    exchanges = parse_omp_pi_exchanges(session, min_chars=1)

    assert [ex.user_content for ex in exchanges] == [
        "list_dir を Result 型にして",
        "legacy を core 配下へ移動して",
    ]
    # assistant の text ブロックのみ。thinking や toolCall の中身は入らない
    assert exchanges[0].agent_content == ""
    assert exchanges[1].agent_content == "list_dir を Result 型に変更しました。"


def test_index_file_records_omp_pi_touches_for_relative_paths(tmp_path: Path) -> None:
    """相対パスのままだと不変条件3で全部落ちる。cwd で絶対化できていることを確認する。"""
    project_root = tmp_path / "project"
    source_dir = project_root / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")
    (source_dir / "result.py").write_text("class Result:\n    pass\n")
    (source_dir / "limits.py").write_text("DEFAULT_BUCKET = 30\n")

    session = tmp_path / "session.jsonl"
    _write_session(session, project_root)
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    indexed = index_file(
        session,
        db_path,
        min_chars=1,
        project_root=project_root,
        harness="omp-pi",
    )

    assert indexed == 2
    con = get_connection(db_path)
    touched = {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT file_path FROM code_touches WHERE harness = 'omp-pi'"
        )
    }
    # ヘッダ由来の相対パス・write の相対 path・複数ファイルパッチのすべてが揃う
    assert touched == {
        "src/fs.py",
        "src/result.py",
        "src/types.ts",
        "src/core/util.ts",
        "src/limits.py",
        "src/legacy.py",
    }
    # anchor capability なので行粒度には上がらず、ファイル粒度で必ず1本張る（不変条件2）
    granularities = {
        row[0] for row in con.execute("SELECT DISTINCT granularity FROM code_edges")
    }
    assert granularities == {"file"}
    con.close()


def test_index_file_records_omp_pi_move_as_file_rename(tmp_path: Path) -> None:
    """パッチ本文の MV は §8.2 段1 の改名記録として残す。"""
    project_root = tmp_path / "project"
    (project_root / "src" / "core").mkdir(parents=True)
    session = tmp_path / "session.jsonl"
    _write_session(session, project_root)
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)

    index_file(
        session, db_path, min_chars=1, project_root=project_root, harness="omp-pi"
    )

    con = get_connection(db_path)
    rows = con.execute("SELECT old_path, new_path, source FROM file_renames").fetchall()
    assert [tuple(row) for row in rows] == [
        ("src/legacy.py", "src/core/legacy.py", "harness")
    ]
    con.close()


def test_index_file_omp_pi_is_incremental(tmp_path: Path) -> None:
    """同じセッションを再実行しても exchange を重複登録しない。"""
    session = tmp_path / "session.jsonl"
    _write_session(session, tmp_path)
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    assert index_file(session, db_path, min_chars=1, harness="omp-pi") == 2
    assert index_file(session, db_path, min_chars=1, harness="omp-pi") == 0


def test_index_file_omp_pi_finds_cwd_after_placeholder_truncation(
    tmp_path: Path,
) -> None:
    """増分インデックスで session 行が None に潰れても cwd を読み直せる。

    cwd を見失うと相対パスが絶対化できず、2ターン目以降の編集記録が静かに落ちる。
    1ターン目だけを先にインデックスし、2ターン目の src/legacy.py が拾えるかで検証する。
    """
    project_root = tmp_path / "project"
    source_dir = project_root / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "fs.py").write_text("def list_dir(path):\n    return path\n")

    session = tmp_path / "session.jsonl"
    original = _FIXTURE.read_text().replace('"/repo', f'"{project_root}')
    lines = original.splitlines(keepends=True)
    # 2ターン目のユーザー発話（index 11）の手前までを先に取り込む
    session.write_text("".join(lines[:11]))
    db_path = project_root / ".codeatrium" / "memory.db"
    init_db(db_path)
    assert (
        index_file(
            session, db_path, min_chars=1, project_root=project_root, harness="omp-pi"
        )
        == 1
    )

    # 残りを追記して再インデックス。session 行（index 1）はもう None に潰れている
    session.write_text(original)
    assert (
        index_file(
            session, db_path, min_chars=1, project_root=project_root, harness="omp-pi"
        )
        == 1
    )

    con = get_connection(db_path)
    touched = {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT file_path FROM code_touches WHERE harness = 'omp-pi'"
        )
    }
    con.close()
    # 2ターン目で初めて登場するファイル。cwd が引けていなければ記録されない
    assert "src/legacy.py" in touched
