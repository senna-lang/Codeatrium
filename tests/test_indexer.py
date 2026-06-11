"""
.jsonl パース・exchange 分割・DB 保存のテスト
"""

import json
from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_file, parse_exchanges

# ---- フィクスチャ ----


def make_user_entry(
    uuid: str, text: str, parent_uuid: str | None = None, is_meta: bool = False, git_branch: str | None = None
) -> dict:
    entry = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "isMeta": is_meta,
        "timestamp": "2026-03-26T00:00:00.000Z",
        "message": {"role": "user", "content": text},
    }
    if git_branch is not None:
        entry["gitBranch"] = git_branch
    return entry


def make_assistant_entry(uuid: str, text: str, parent_uuid: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": "2026-03-26T00:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def make_assistant_entry_with_tool_use(
    uuid: str, file_path: str, parent_uuid: str | None = None, tool_name: str = "Edit"
) -> dict:
    """Assistant entry with a tool_use block (no text content)"""
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": "2026-03-26T00:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": tool_name, "input": {key: file_path}}],
        },
    }


def write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ---- parse_exchanges のテスト ----


def test_parse_exchanges_single(tmp_path: Path) -> None:
    """1 user + 1 assistant = 1 exchange"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert "connection pool" in exchanges[0].user_content
    assert "pool_size" in exchanges[0].agent_content


def test_parse_exchanges_git_branch_captured(tmp_path: Path) -> None:
    """git_branch が capture される"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5, git_branch="main"),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert exchanges[0].git_branch == "main"


def test_parse_exchanges_git_branch_missing_is_none(tmp_path: Path) -> None:
    """git_branch が missing の場合は None になる"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert exchanges[0].git_branch is None


def test_parse_exchanges_git_branch_empty_string_is_none(tmp_path: Path) -> None:
    """git_branch が empty string の場合は None になる"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5, git_branch=""),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert exchanges[0].git_branch is None


def test_parse_exchanges_branch_per_exchange(tmp_path: Path) -> None:
    """各 exchange が異なる git_branch を持つことができる"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "最初の質問です。よろしくお願いします。" * 5, git_branch="main"),
            make_assistant_entry("a1", "了解しました。詳しく説明します。" * 5, "u1"),
            make_user_entry("u2", "次の質問です。詳しく教えてください。" * 5, "a1", git_branch="release/1.0-hardening"),
            make_assistant_entry(
                "a2", "詳しく説明します。ご参考になれば幸いです。" * 5, "u2"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 2
    assert exchanges[0].git_branch == "main"
    assert exchanges[1].git_branch == "release/1.0-hardening"


def test_index_file_persists_git_branch(tmp_path: Path) -> None:
    """index_file が git_branch を DB に保存する"""
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5, git_branch="feature-x"),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )

    index_file(jsonl, db_path)

    con = get_connection(db_path)
    rows = con.execute("SELECT git_branch FROM exchanges").fetchall()
    assert len(rows) == 1
    assert rows[0]["git_branch"] == "feature-x"
    con.close()


def test_parse_exchanges_multiple(tmp_path: Path) -> None:
    """2 user turn = 2 exchange"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "最初の質問です。よろしくお願いします。" * 5),
            make_assistant_entry("a1", "了解しました。詳しく説明します。" * 5, "u1"),
            make_user_entry("u2", "次の質問です。詳しく教えてください。" * 5, "a1"),
            make_assistant_entry(
                "a2", "詳しく説明します。ご参考になれば幸いです。" * 5, "u2"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 2


def test_parse_exchanges_skips_trivial(tmp_path: Path) -> None:
    """50文字未満の exchange は除外"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "OK"),
            make_assistant_entry("a1", "了解", "u1"),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 0


def test_parse_exchanges_skips_meta(tmp_path: Path) -> None:
    """isMeta=True の user メッセージは exchange 境界にならない"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u0", "/clear", is_meta=True),
            make_user_entry("u1", "本物の質問です。" * 10),
            make_assistant_entry("a1", "回答します。" * 10, "u1"),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1


def test_parse_exchanges_ply_range(tmp_path: Path) -> None:
    """ply_start と ply_end が正しく設定される"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "質問です。" * 20),
            make_assistant_entry("a1", "回答します。" * 20, "u1"),
        ],
    )
    exchanges = parse_exchanges(f)
    assert exchanges[0].ply_start == 0
    assert exchanges[0].ply_end == 1


def test_parse_exchanges_skips_old_exchanges(tmp_path: Path) -> None:
    """last_ply_end 以前の ply は exchange として再構築しない（None プレースホルダ）"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "最初の質問です。よろしくお願いします。" * 5),
            make_assistant_entry("a1", "了解しました。詳しく説明します。" * 5, "u1"),
            make_user_entry("u2", "次の質問です。詳しく教えてください。" * 5, "a1"),
            make_assistant_entry(
                "a2", "詳しく説明します。ご参考になれば幸いです。" * 5, "u2"
            ),
        ],
    )
    exchanges = parse_exchanges(f, last_ply_end=1)
    # ply 0, 1 は再構築されず、ply 2, 3 の1件の exchange のみ返る
    assert len(exchanges) == 1
    assert exchanges[0].ply_start == 2
    assert exchanges[0].ply_end == 3


def test_parse_exchanges_malformed_line_in_indexed_region_no_drift(
    tmp_path: Path,
) -> None:
    """既インデックス領域に壊れた JSON 行があってもスキップ境界がズレない（回帰）。

    壊れた行は成功パース座標系に位置を持たないため、skip 領域でも数えてはならない。
    """
    f = tmp_path / "session.jsonl"
    # ply 0(user), 1(assistant) を全行パース時の last_ply_end とする。
    # 行頭に壊れた JSON を 1 行混ぜると、座標系を誤ると境界が 1 つ早くズレる。
    lines = [
        json.dumps(make_user_entry("u1", "最初の質問です。" * 6)),
        "{ this is not valid json",
        json.dumps(make_assistant_entry("a1", "了解しました。" * 6, "u1")),
        json.dumps(make_user_entry("u2", "次の質問です。" * 6, "a1")),
        json.dumps(make_assistant_entry("a2", "説明します。" * 6, "u2")),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 全行パース（last_ply_end=-1）での境界を基準にする
    full = parse_exchanges(f, last_ply_end=-1)
    # 壊れた行は無視され、u1/a1 と u2/a2 の 2 exchange になる
    assert [(e.ply_start, e.ply_end) for e in full] == [(0, 1), (2, 3)]

    # 最初の exchange(ply 0,1)までインデックス済みとして再パース
    incremental = parse_exchanges(f, last_ply_end=1)
    # 2 番目の exchange(ply 2,3)だけが返り、座標がドリフトしない
    assert [(e.ply_start, e.ply_end) for e in incremental] == [(2, 3)]


def test_parse_exchanges_deterministic_id(tmp_path: Path) -> None:
    """同じファイルを2回パースすると同じ exchange_id になる"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "同じクエリです。" * 15),
            make_assistant_entry("a1", "同じ回答です。" * 15, "u1"),
        ],
    )
    exchanges1 = parse_exchanges(f)
    exchanges2 = parse_exchanges(f)
    assert exchanges1[0].id == exchanges2[0].id


# ---- index_file のテスト ----


def test_index_file_inserts_to_db(tmp_path: Path) -> None:
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )

    index_file(jsonl, db_path)

    con = get_connection(db_path)
    rows = con.execute("SELECT * FROM exchanges").fetchall()
    assert len(rows) == 1
    con.close()


def test_index_file_dedup(tmp_path: Path) -> None:
    """同じファイルを2回 index しても exchange は重複しない"""
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "重複テストです。" * 15),
            make_assistant_entry("a1", "重複しません。" * 15, "u1"),
        ],
    )

    index_file(jsonl, db_path)
    count = index_file(jsonl, db_path)

    assert count == 0  # 2回目は新規 exchange なし
    con = get_connection(db_path)
    rows = con.execute("SELECT * FROM exchanges").fetchall()
    assert len(rows) == 1
    con.close()


def test_index_file_incremental(tmp_path: Path) -> None:
    """セッション途中で追記された exchange が差分インデックスされる"""
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    # 初回: 1 exchange
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "最初の質問です。よろしくお願いします。" * 5),
            make_assistant_entry("a1", "了解しました。詳しく説明します。" * 5, "u1"),
        ],
    )
    count1 = index_file(jsonl, db_path)
    assert count1 == 1

    # 追記: 2つ目の exchange を追加
    with jsonl.open("a") as f:
        f.write(
            json.dumps(
                make_user_entry("u2", "次の質問です。詳しく教えてください。" * 5, "a1"),
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                make_assistant_entry(
                    "a2", "詳しく説明します。ご参考になれば幸いです。" * 5, "u2"
                ),
                ensure_ascii=False,
            )
            + "\n"
        )

    count2 = index_file(jsonl, db_path)
    assert count2 == 1  # 新規の1件だけ

    con = get_connection(db_path)
    rows = con.execute("SELECT * FROM exchanges").fetchall()
    assert len(rows) == 2  # 合計2件
    con.close()


def test_parse_exchanges_excludes_compaction_content(tmp_path: Path) -> None:
    """コンパクション要約とその直後の assistant 応答は exchange content から除外される"""
    f = tmp_path / "session.jsonl"
    compaction_text = (
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier portion."
    )
    compaction_response = (
        "I'll continue from where we left off. "
        "Previously we discussed connection pooling and database optimization. " * 5
    )
    write_jsonl(
        f,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
            # コンパクション要約（exchange 境界にならない）
            make_user_entry("u_compact", compaction_text, "a1"),
            # コンパクション直後の assistant 応答（除外されるべき）
            make_assistant_entry("a_compact", compaction_response, "u_compact"),
            # 次の実質的な exchange
            make_user_entry("u2", "次にインデックスの最適化について教えて。" * 5, "a_compact"),
            make_assistant_entry(
                "a2", "CREATE INDEX を使うと検索が高速化されます。" * 5, "u2"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 2

    # exchange 1: コンパクション要約と応答が agent_content に含まれない
    assert "pool_size" in exchanges[0].agent_content
    assert "continued from" not in exchanges[0].agent_content
    assert "Previously we discussed" not in exchanges[0].agent_content

    # exchange 2: 通常通り
    assert "インデックス" in exchanges[1].user_content
    assert "CREATE INDEX" in exchanges[1].agent_content


def test_index_file_fts_populated(tmp_path: Path) -> None:
    """FTS インデックスに内容が入る"""
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "connection pool の修正を教えてください。" * 5),
            make_assistant_entry(
                "a1", "pool_size=5 を DATABASE_URL に追加してください。" * 5, "u1"
            ),
        ],
    )
    index_file(jsonl, db_path)

    con = get_connection(db_path)
    rows = con.execute(
        "SELECT rowid FROM exchanges_fts WHERE exchanges_fts MATCH 'pool_size'"
    ).fetchall()
    assert len(rows) == 1
    con.close()


# ---- tool_use ファイル抽出テスト ----


def test_parse_exchanges_captures_tool_use_files(tmp_path: Path) -> None:
    """tool_use Edit ブロックのファイルパスが exchange.files に含まれる"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "Edit the source file please. " * 10),
            make_assistant_entry_with_tool_use("a1", "src/foo.py", "u1"),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert "src/foo.py" in exchanges[0].files


def test_parse_exchanges_tool_use_dedup(tmp_path: Path) -> None:
    """複数の assistant エントリが同じファイルを参照する場合、重複を除外する"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "Edit the source file please. " * 10),
            make_assistant_entry_with_tool_use("a1", "src/foo.py", "u1"),
            make_assistant_entry_with_tool_use("a2", "src/foo.py", "a1"),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert exchanges[0].files.count("src/foo.py") == 1
    assert len(exchanges[0].files) == 1


def test_parse_exchanges_tool_use_excludes_external(tmp_path: Path) -> None:
    """外部パス（.venv など）は除外される"""
    f = tmp_path / "session.jsonl"
    write_jsonl(
        f,
        [
            make_user_entry("u1", "Edit the source file please. " * 10),
            make_assistant_entry_with_tool_use(
                "a1", "/Users/u/.venv/lib/python3.12/site-packages/foo.py", "u1"
            ),
        ],
    )
    exchanges = parse_exchanges(f)
    assert len(exchanges) == 1
    assert "/Users/u/.venv/lib/python3.12/site-packages/foo.py" not in exchanges[0].files
    assert len(exchanges[0].files) == 0


def test_index_file_writes_exchange_files(tmp_path: Path) -> None:
    """index_file が exchange_files テーブルに書き込む"""
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "Edit the source file please. " * 10),
            make_assistant_entry_with_tool_use("a1", "src/bar.py", "u1"),
        ],
    )

    index_file(jsonl, db_path)

    con = get_connection(db_path)
    rows = con.execute("SELECT file_path FROM exchange_files").fetchall()
    assert "src/bar.py" in [r[0] for r in rows]
    con.close()


def test_index_file_exchange_files_dedup(tmp_path: Path) -> None:
    """同じ exchange 内の複数の tool_use ブロックでも exchange_files は重複しない"""
    db_path = tmp_path / ".codeatrium" / "memory.db"
    init_db(db_path)

    jsonl = tmp_path / "session.jsonl"
    write_jsonl(
        jsonl,
        [
            make_user_entry("u1", "Edit the source file please. " * 10),
            make_assistant_entry_with_tool_use("a1", "src/baz.py", "u1"),
            make_assistant_entry_with_tool_use("a2", "src/baz.py", "a1"),
        ],
    )

    index_file(jsonl, db_path)

    con = get_connection(db_path)
    count = con.execute("SELECT COUNT(*) FROM exchange_files").fetchone()[0]
    assert count == 1
    con.close()
