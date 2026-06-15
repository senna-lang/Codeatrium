"""
蒸留モジュールのテスト

call_claude・Embedder はモックしてモデルロードを避ける
"""

import hashlib
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from codeatrium.db import get_connection, init_db
from codeatrium.distiller import (
    PalaceObject,
    distill_all,
    distill_exchange,
    extract_files_touched,
    save_palace_object,
)
from codeatrium.llm import DistillBackend

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
    # 会話に2件以上の exchange を確保（min_exchanges=2 フィルタ対策）
    con.execute(
        """
        INSERT OR IGNORE INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content, distilled_at, distill_status)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        ("_pad_conv1", "conv1", 0, 1, "padding", "padding", "2026-01-01", "distilled"),
    )
    con.execute(
        """
        INSERT OR IGNORE INTO exchanges
            (id, conversation_id, ply_start, ply_end, user_content, agent_content)
        VALUES (?,?,?,?,?,?)
        """,
        (ex_id, "conv1", 2, 5, user_text, agent_text),
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


def test_extract_files_excludes_site_packages() -> None:
    result = extract_files_touched(
        "/opt/anaconda3/lib/python3.11/site-packages/sklearn/base.py", ""
    )
    assert result == []


def test_extract_files_excludes_stdlib() -> None:
    result = extract_files_touched(
        "/opt/anaconda3/lib/python3.11/urllib/request.py", ""
    )
    assert result == []


def test_extract_files_excludes_venv() -> None:
    result = extract_files_touched(
        ".venv/lib/python3.11/site-packages/typer/main.py", ""
    )
    assert result == []


def test_extract_files_excludes_node_modules() -> None:
    result = extract_files_touched("node_modules/react/index.js", "")
    assert result == []


def test_extract_files_keeps_project_files_alongside_external() -> None:
    """外部パスは除外しつつプロジェクトファイルは残る"""
    result = extract_files_touched(
        "src/app.py /usr/lib/python3/os.py src/util/helper.py", ""
    )
    assert "src/app.py" in result
    assert "src/util/helper.py" in result
    assert len(result) == 2


def test_extract_files_project_root_filters_absolute() -> None:
    """project_root 指定時、配下の絶対パスは残り外部は除外"""
    result = extract_files_touched(
        "/home/user/myproject/src/app.py /opt/anaconda3/lib/python3.11/os.py",
        "/home/user/myproject/tests/test_app.py",
        project_root="/home/user/myproject",
    )
    assert "/home/user/myproject/src/app.py" in result
    assert "/home/user/myproject/tests/test_app.py" in result
    assert len(result) == 2


def test_extract_files_project_root_unknown_external() -> None:
    """ハードコードマーカーにない外部パスも git root で除外される"""
    result = extract_files_touched(
        "/home/user/.local/lib/python3.11/foo/bar.py",
        "",
        project_root="/home/user/myproject",
    )
    assert result == []


def test_extract_files_relative_paths_unaffected_by_root() -> None:
    """相対パスは project_root に影響されずマーカーのみでフィルタ"""
    result = extract_files_touched(
        "src/app.py node_modules/react/index.js",
        "",
        project_root="/home/user/myproject",
    )
    assert result == ["src/app.py"]


# --- distill_exchange ---


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_returns_palace(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    palace = distill_exchange(
        "ex1", db_path, "pool の設定", "pool_size=5 を追加した", 0, 3
    )
    assert palace.exchange_core == "pool_size を 5 に設定した"
    assert palace.specific_context == "pool_size=5"
    assert len(palace.room_assignments) == 1


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_calls_claude_once(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    distill_exchange("ex1", db_path, "pool の設定", "pool_size=5", 0, 3)
    mock_call.assert_called_once()


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_extracts_files(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    palace = distill_exchange(
        "ex1", db_path, "src/db/pool.py を修正", "pool_size=5", 0, 3
    )
    assert "src/db/pool.py" in palace.files_touched


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_merges_exchange_files(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")
    con = get_connection(db_path)
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES (?,?)",
        ("ex1", "src/tool.py"),
    )
    con.commit()
    con.close()
    palace = distill_exchange("ex1", db_path, "no paths here", "none", 0, 3)
    assert "src/tool.py" in palace.files_touched


# --- save_palace_object ---


def test_save_palace_object_symbol_id_uses_3part_hash(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(
        db_path, "ex1", user_text="Foo.bar method", agent_text="Foo.bar implementation"
    )

    resolver = MagicMock()
    sym = MagicMock()
    sym.symbol_name = "Foo.bar"
    sym.file_path = "src/foo.py"
    sym.symbol_kind = "method"
    sym.signature = "def bar"
    sym.line = 1
    resolver.extract.return_value = [sym]

    palace = PalaceObject(
        exchange_core="c",
        specific_context="s",
        room_assignments=[],
        files_touched=["src/foo.py"],
    )
    save_palace_object(
        db_path, "ex1", palace, np.zeros(384, dtype=np.float32), resolver=resolver
    )

    con = get_connection(db_path)
    row = con.execute("SELECT id, dedup_hash FROM symbols").fetchone()
    con.close()

    palace_id = hashlib.sha256(b"palace:ex1").hexdigest()
    expected_id = hashlib.sha256(f"Foo.bar:src/foo.py:{palace_id}".encode()).hexdigest()
    expected_dedup = hashlib.sha256(b"Foo.bar:src/foo.py").hexdigest()

    assert row["id"] == expected_id
    assert row["dedup_hash"] == expected_dedup


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


def test_save_palace_object_skips_symbol_not_in_body(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(
        db_path,
        "ex1",
        user_text="some unrelated text " * 5,
        agent_text="more text " * 5,
    )

    resolver = MagicMock()
    sym = MagicMock()
    sym.symbol_name = "NotMentioned"
    sym.file_path = "src/foo.py"
    resolver.extract.return_value = [sym]

    palace = PalaceObject(
        exchange_core="c",
        specific_context="s",
        room_assignments=[],
        files_touched=["src/foo.py"],
    )
    save_palace_object(
        db_path, "ex1", palace, np.zeros(384, dtype=np.float32), resolver=resolver
    )

    con = get_connection(db_path)
    count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    con.close()

    assert count == 0


def test_save_palace_object_includes_symbol_in_body(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(
        db_path, "ex1", user_text="Foo.bar method " * 5, agent_text="more text " * 5
    )

    resolver = MagicMock()
    sym = MagicMock()
    sym.symbol_name = "Foo.bar"
    sym.file_path = "src/foo.py"
    sym.symbol_kind = "method"
    sym.signature = "def bar"
    sym.line = 1
    resolver.extract.return_value = [sym]

    palace = PalaceObject(
        exchange_core="c",
        specific_context="s",
        room_assignments=[],
        files_touched=["src/foo.py"],
    )
    save_palace_object(
        db_path, "ex1", palace, np.zeros(384, dtype=np.float32), resolver=resolver
    )

    con = get_connection(db_path)
    count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    con.close()

    assert count == 1


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


def test_save_palace_object_two_palace_objects_same_symbol(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(
        db_path,
        "ex1",
        user_text="Foo.bar method " * 5,
        agent_text="implementation " * 5,
    )
    _make_exchange(
        db_path,
        "ex2",
        user_text="Foo.bar method " * 5,
        agent_text="implementation " * 5,
    )

    resolver = MagicMock()
    sym = MagicMock()
    sym.symbol_name = "Foo.bar"
    sym.file_path = "src/foo.py"
    sym.symbol_kind = "method"
    sym.signature = "def bar"
    sym.line = 1
    resolver.extract.return_value = [sym]

    palace = PalaceObject(
        exchange_core="c",
        specific_context="s",
        room_assignments=[],
        files_touched=["src/foo.py"],
    )

    save_palace_object(
        db_path, "ex1", palace, np.zeros(384, dtype=np.float32), resolver=resolver
    )
    save_palace_object(
        db_path, "ex2", palace, np.zeros(384, dtype=np.float32), resolver=resolver
    )

    con = get_connection(db_path)
    all_rows = con.execute("SELECT id, dedup_hash FROM symbols").fetchall()
    con.close()

    assert len(all_rows) == 2
    ids = {row["id"] for row in all_rows}
    dedup_hashes = {row["dedup_hash"] for row in all_rows}
    assert len(ids) == 2  # Two different ids
    assert len(dedup_hashes) == 1  # Same dedup_hash


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


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_processes_undistilled(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("codeatrium.distiller.Embedder", return_value=mock_embedder):
        count, _ = distill_all(db_path)

    assert count == 1


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_skips_distilled(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    con = get_connection(db_path)
    con.execute(
        "UPDATE exchanges SET distilled_at = '2026-01-01', distill_status = 'distilled' WHERE id = 'ex1'"
    )
    con.commit()
    con.close()

    mock_embedder = MagicMock()
    with patch("codeatrium.distiller.Embedder", return_value=mock_embedder):
        count, _ = distill_all(db_path)

    assert count == 0


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_returns_count(mock_call, tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")
    _make_exchange(db_path, "ex2")

    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("codeatrium.distiller.Embedder", return_value=mock_embedder):
        count, _ = distill_all(db_path)

    assert count == 2


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_returns_tuple(mock_call, tmp_path) -> None:
    """distill_all は tuple を返す"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("codeatrium.distiller.Embedder", return_value=mock_embedder):
        result = distill_all(db_path)

    assert isinstance(result, tuple)
    assert len(result) == 2


@patch("codeatrium.distiller.call_claude")
def test_distill_all_error_count(mock_call, tmp_path) -> None:
    """distill_all はエラー数をカウントして返す"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")
    _make_exchange(db_path, "ex2")

    # 1回目は失敗、2回目は成功
    mock_call.side_effect = [RuntimeError("Test error"), MOCK_PALACE_RESPONSE]

    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("codeatrium.distiller.Embedder", return_value=mock_embedder):
        count, errors = distill_all(db_path)

    assert count == 1
    assert errors == 1


def test_save_palace_object_sets_distill_status_distilled(tmp_path) -> None:
    """save_palace_object は distill_status を 'distilled' にセットする"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    palace = PalaceObject(
        exchange_core="蒸留済み",
        specific_context="detail",
        room_assignments=[],
    )
    save_palace_object(db_path, "ex1", palace, np.zeros(384, dtype=np.float32))

    con = get_connection(db_path)
    row = con.execute(
        "SELECT distill_status FROM exchanges WHERE id=?", ("ex1",)
    ).fetchone()
    assert row["distill_status"] == "distilled"
    con.close()


def test_save_palace_object_raises_on_palace_insert_failure(tmp_path) -> None:
    """save_palace_object が palace_objects INSERT 失敗時に例外を raise する"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    # Recreate palace_objects table with NOT NULL bm25_text to simulate legacy schema
    con = get_connection(db_path)
    con.execute("DROP TABLE palace_objects")
    con.execute("""
        CREATE TABLE palace_objects (
            id TEXT PRIMARY KEY,
            exchange_id TEXT NOT NULL,
            exchange_core TEXT NOT NULL,
            specific_context TEXT NOT NULL,
            distill_text TEXT NOT NULL,
            bm25_text TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

    palace = PalaceObject(
        exchange_core="テスト",
        specific_context="detail",
        room_assignments=[],
        files_touched=[],
    )

    with pytest.raises(Exception):
        save_palace_object(db_path, "ex1", palace, np.zeros(384, dtype=np.float32))

    con = get_connection(db_path)
    # distill_status should still be 'pending' due to rollback
    row = con.execute(
        "SELECT distill_status FROM exchanges WHERE id=?", ("ex1",)
    ).fetchone()
    assert row["distill_status"] == "pending"

    # palace_objects should have 0 rows
    palace_rows = con.execute("SELECT * FROM palace_objects").fetchall()
    assert len(palace_rows) == 0
    con.close()


def test_save_palace_object_rollback_on_error(tmp_path) -> None:
    """save_palace_object がエラーで失敗した場合、distill_status は 'pending' で palace_objects は空"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    palace = PalaceObject(
        exchange_core="テスト",
        specific_context="detail",
        room_assignments=[],
    )

    # 不正な次元の embedding を渡して struct.pack を失敗させる
    bad_embedding = np.zeros(100, dtype=np.float32)

    try:
        save_palace_object(db_path, "ex1", palace, bad_embedding)
    except Exception:
        pass  # エラーは予期されている

    con = get_connection(db_path)
    # distill_status は 'pending' のまま
    row = con.execute(
        "SELECT distill_status FROM exchanges WHERE id=?", ("ex1",)
    ).fetchone()
    assert row["distill_status"] == "pending"

    # palace_objects テーブルは空
    palace_rows = con.execute("SELECT * FROM palace_objects").fetchall()
    assert len(palace_rows) == 0
    con.close()


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_accepts_backend(mock_call, tmp_path) -> None:
    """distill_exchange は backend パラメータを受け取れる"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    backend = DistillBackend(
        provider="claude", model="claude-haiku-4-5-20251001", base_url=None
    )
    palace = distill_exchange(
        "ex1", db_path, "pool の設定", "pool_size=5", 0, 3, backend=backend
    )
    assert palace.exchange_core == "pool_size を 5 に設定した"
    mock_call.assert_called_once()


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_all_accepts_backend(mock_call, tmp_path) -> None:
    """distill_all は backend パラメータを受け取れる"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    _make_exchange(db_path, "ex1")

    backend = DistillBackend(
        provider="claude", model="claude-haiku-4-5-20251001", base_url=None
    )
    mock_embedder = MagicMock()
    mock_embedder.embed_passage.return_value = np.zeros(384, dtype=np.float32)

    with patch("codeatrium.distiller.Embedder", return_value=mock_embedder):
        count, _ = distill_all(db_path, backend=backend)

    assert count == 1


@patch("codeatrium.distiller.call_claude", return_value=MOCK_PALACE_RESPONSE)
def test_distill_exchange_patch_point_unchanged(mock_call, tmp_path) -> None:
    """patch('codeatrium.distiller.call_claude') がパッチ対象として機能し続ける（回帰防止）"""
    db_path = tmp_path / "memory.db"
    init_db(db_path)
    palace = distill_exchange("ex1", db_path, "pool の設定", "pool_size=5", 0, 3)
    assert palace.exchange_core == "pool_size を 5 に設定した"
    mock_call.assert_called_once()
