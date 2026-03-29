"""
蒸留モジュールのテスト

call_claude・Embedder はモックしてモデルロードを避ける
"""

from unittest.mock import MagicMock, patch

import numpy as np

from logo.db import get_connection, init_db
from logo.distiller import (
    PalaceObject,
    distill_all,
    distill_exchange,
    extract_files_touched,
    save_palace_object,
)

# --- フィクスチャ ---

MOCK_PALACE_RESPONSE = {
    "exchange_core": "pool_size を 5 に設定した",
    "specific_context": "pool_size=5",
    "room_assignments": [
        {
            "room_type": "concept",
            "room_key": "db-pool",
            "room_label": "DB Pool",
            "relevance": 0.9,
        }
    ],
}

LONG_TEXT = "テスト発話 " * 20  # 100文字以上


def _make_exchange(db_path, ex_id, user_text=LONG_TEXT, agent_text=LONG_TEXT):
    con = get_connection(db_path)
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?,?)",
        ("conv1", "/path/to.jsonl"),
    )
    con.execute(
        """
        INSERT OR IGNORE INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content)
        VALUES (?,?,?,?,?,?)
        """,
        (ex_id, "conv1", 0, 3, user_text, agent_text),
    )
    con.commit()
    con.close()


# --- extract_files_touched ---


def test_extract_files_relative_path() -> None:
    result = extract_files_touched("src/auth/middleware.py を修正した", "")
    assert "src/auth/middleware.py" in result


def test_extract_files_absolute_path() -> None:
    result = extract_files_touched("/Users/foo/project/db.py", "")
    assert "/Users/foo/project/db.py" in result


def test_extract_files_in_agent_content() -> None:
    result = extract_files_touched("", "lib/db/pool.ts を更新した")
    assert "lib/db/pool.ts" in result


def test_extract_files_no_match() -> None:
    result = extract_files_touched("ランダムテキスト", "ファイルなし")
    assert result == []


def test_extract_files_dedup() -> None:
    result = extract_files_touched("src/foo.py src/foo.py", "")
    assert result.count("src/foo.py") == 1


# --- distill_exchange ---


@patch("logo.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_returns_palace(mock_call) -> None:
    palace = distill_exchange("ex1", "pool の設定", "pool_size=5 を追加した", 0, 3)
    assert palace.exchange_core == "pool_size を 5 に設定した"
    assert palace.specific_context == "pool_size=5"
    assert len(palace.room_assignments) == 1


@patch("logo.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_calls_claude_once(mock_call) -> None:
    distill_exchange("ex1", "pool の設定", "pool_size=5", 0, 3)
    mock_call.assert_called_once()


@patch("logo.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_extracts_files(mock_call) -> None:
    palace = distill_exchange("ex1", "src/db/pool.py を修正", "pool_size=5", 0, 3)
    assert "src/db/pool.py" in palace.files_touched


# --- save_palace_object ---


def test_save_palace_object_stores_in_db(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    palace = PalaceObject(
        exchange_core="テストをした",
        specific_context="test=true",
        room_assignments=[
            {
                "room_type": "concept",
                "room_key": "test",
                "room_label": "Test",
                "relevance": 0.8,
            }
        ],
    )
    vec = np.zeros(384, dtype=np.float32)
    save_palace_object(db_path, "ex1", palace, vec)

    con = get_connection(db_path)
    row = con.execute(
        "SELECT * FROM palace_objects WHERE exchange_id=?", ("ex1",)
    ).fetchone()
    assert row is not None
    assert row["exchange_core"] == "テストをした"
    con.close()


def test_save_palace_object_sets_distilled_at(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    palace = PalaceObject(
        exchange_core="done",
        specific_context="detail",
        room_assignments=[],
    )
    save_palace_object(db_path, "ex1", palace, np.zeros(384, dtype=np.float32))

    con = get_connection(db_path)
    row = con.execute(
        "SELECT distilled_at FROM exchanges WHERE id=?", ("ex1",)
    ).fetchone()
    assert row["distilled_at"] is not None
    con.close()


def test_save_palace_object_saves_rooms(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    palace = PalaceObject(
        exchange_core="done",
        specific_context="detail",
        room_assignments=[
            {
                "room_type": "concept",
                "room_key": "auth",
                "room_label": "Auth",
                "relevance": 0.9,
            }
        ],
    )
    save_palace_object(db_path, "ex1", palace, np.zeros(384, dtype=np.float32))

    con = get_connection(db_path)
    rows = con.execute("SELECT * FROM rooms").fetchall()
    assert len(rows) == 1
    assert rows[0]["room_key"] == "auth"
    con.close()


def test_save_palace_object_saves_vec(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    palace = PalaceObject(
        exchange_core="done",
        specific_context="detail",
        room_assignments=[],
    )
    save_palace_object(db_path, "ex1", palace, np.zeros(384, dtype=np.float32))

    con = get_connection(db_path)
    row = con.execute("SELECT palace_id FROM vec_palace").fetchone()
    assert row is not None
    con.close()


# --- distill_all ---


@patch("logo.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_processes_undistilled(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("logo.distiller.Embedder", return_value=mock_embedder):
        count = distill_all(db_path)

    assert count == 1


@patch("logo.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_skips_distilled(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    con = get_connection(db_path)
    con.execute("UPDATE exchanges SET distilled_at = '2026-01-01' WHERE id = 'ex1'")
    con.commit()
    con.close()

    mock_embedder = MagicMock()
    with patch("logo.distiller.Embedder", return_value=mock_embedder):
        count = distill_all(db_path)

    assert count == 0


@patch("logo.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_returns_count(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")
    _make_exchange(db_path, "ex2")

    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("logo.distiller.Embedder", return_value=mock_embedder):
        count = distill_all(db_path)

    assert count == 2
