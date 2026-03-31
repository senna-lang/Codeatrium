"""
.jsonl パース・exchange 分割・DB 保存のテスト
"""

import json
from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.indexer import index_file, parse_exchanges

# ---- フィクスチャ ----


def make_user_entry(
    uuid: str, text: str, parent_uuid: str | None = None, is_meta: bool = False
) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "isMeta": is_meta,
        "timestamp": "2026-03-26T00:00:00.000Z",
        "message": {"role": "user", "content": text},
    }


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
