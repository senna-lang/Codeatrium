"""
loci show / loci dump --distilled のテスト

show: exchange id から原文を取得。同一会話内の ply 隣接（`context`、confidence 無しの
      additive レーン）も返し、そこに載る exchange_id で loci show を繰り返せば
      任意の深さまで前後を辿れる（design: 案2「loci show --context N」撤回の代替）。
dump: 蒸留済み palace objects を新しい順に出力
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db

runner = CliRunner()

LONG = "x" * 200


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    codeatrium_dir = tmp_path / ".codeatrium"
    codeatrium_dir.mkdir()
    db = codeatrium_dir / "memory.db"
    init_db(db)
    con = get_connection(db)
    return db, con


def _insert_exchange(con, ex_id, conv_id, source_path, ply_start=0):
    con.execute(
        "INSERT OR IGNORE INTO conversations (id, source_path) VALUES (?,?)",
        (conv_id, source_path),
    )
    con.execute(
        """INSERT OR IGNORE INTO exchanges
           (id, conversation_id, ply_start, ply_end, user_content, agent_content)
           VALUES (?,?,?,?,?,?)""",
        (ex_id, conv_id, ply_start, ply_start + 3, LONG, "agent response " + LONG),
    )
    con.commit()


def _insert_palace(con, palace_id, exchange_id, core, distilled_at):
    con.execute(
        """INSERT OR IGNORE INTO palace_objects
           (id, exchange_id, exchange_core, specific_context, distill_text)
           VALUES (?,?,?,?,?)""",
        (palace_id, exchange_id, core, "detail", core),
    )
    vec = np.ones(384, dtype=np.float32)
    blob = struct.pack("384f", *vec.tolist())
    con.execute(
        "INSERT OR IGNORE INTO vec_palace (palace_id, embedding) VALUES (?,?)",
        (palace_id, blob),
    )
    con.execute(
        "UPDATE exchanges SET distilled_at = ? WHERE id = ?",
        (distilled_at, exchange_id),
    )
    con.commit()


# ---- loci show ----


def test_show_unknown_exchange_id_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _db, con = _setup(tmp_path)
    con.close()
    result = runner.invoke(app, ["show", "missing-exchange"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_show_returns_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    source = "/fake/session.jsonl"
    _insert_exchange(con, "ex1", "conv1", source, ply_start=5)
    con.close()

    result = runner.invoke(app, ["show", "ex1"])
    assert result.exit_code == 0
    assert LONG in result.output


def test_show_json_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    source = "/fake/session.jsonl"
    _insert_exchange(con, "ex1", "conv1", source, ply_start=0)
    con.close()

    result = runner.invoke(app, ["show", "ex1", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["exchange_id"] == "ex1"
    assert "user_content" in data
    assert "agent_content" in data
    assert "ply_start" in data



def test_show_json_includes_ply_adjacent_context_for_traversal(tmp_path, monkeypatch):
    """design: 前後を辿れるよう、show の出力に同一会話の ply 隣接を additive に乗せる"""
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    source = "/fake/session.jsonl"
    _insert_exchange(con, "ex0", "conv1", source, ply_start=0)
    _insert_exchange(con, "ex1", "conv1", source, ply_start=10)
    _insert_exchange(con, "ex2", "conv1", source, ply_start=20)
    con.close()

    result = runner.invoke(app, ["show", "ex1", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)

    assert data["exchange_id"] == "ex1"
    relations = {(c["relation"], c["exchange_id"]) for c in data["context"]}
    assert relations == {("ply_adjacent", "ex0"), ("ply_adjacent", "ex2")}


def test_show_context_has_no_confidence_field(tmp_path, monkeypatch):
    """context entries は additive な参考情報——confidence を持たない（advisory反映）"""
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    source = "/fake/session.jsonl"
    _insert_exchange(con, "ex0", "conv1", source, ply_start=0)
    _insert_exchange(con, "ex1", "conv1", source, ply_start=10)
    con.close()

    result = runner.invoke(app, ["show", "ex1", "--json"])
    data = json.loads(result.output)

    assert data["context"]
    assert all("confidence" not in c for c in data["context"])


def test_show_context_enables_chained_traversal(tmp_path, monkeypatch):
    """context の exchange_id を使って show を繰り返すと、さらに先へ辿れる"""
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    source = "/fake/session.jsonl"
    for i, ply in enumerate([0, 10, 20, 30, 40]):
        _insert_exchange(con, f"ex{i}", "conv1", source, ply_start=ply)
    con.close()

    # 起点: ex2 (中央) -> 前方の隣接 ex1 に飛ぶ -> そこからさらに ex0 が見える
    hop1 = json.loads(runner.invoke(app, ["show", "ex2", "--json"]).output)
    before_ids = [c["exchange_id"] for c in hop1["context"] if c["relation"] == "ply_adjacent"]
    assert "ex1" in before_ids

    hop2 = json.loads(runner.invoke(app, ["show", "ex1", "--json"]).output)
    hop2_ids = {c["exchange_id"] for c in hop2["context"]}
    assert "ex0" in hop2_ids


def test_show_single_exchange_conversation_has_empty_context(tmp_path, monkeypatch):
    """隣接が存在しない会話では、context は空リストで正直に返す"""
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_exchange(con, "solo", "conv1", "/fake/solo.jsonl", ply_start=0)
    con.close()

    result = runner.invoke(app, ["show", "solo", "--json"])
    data = json.loads(result.output)
    assert data["context"] == []


def test_show_text_output_lists_context_neighbors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    source = "/fake/session.jsonl"
    _insert_exchange(con, "ex0", "conv1", source, ply_start=0)
    _insert_exchange(con, "ex1", "conv1", source, ply_start=10)
    con.close()

    result = runner.invoke(app, ["show", "ex1"])
    assert result.exit_code == 0
    assert "ex0" in result.output

# ---- loci dump --distilled ----


def test_dump_requires_distilled_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    con.close()
    result = runner.invoke(app, ["dump"])
    assert result.exit_code != 0


def test_dump_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    con.close()
    result = runner.invoke(app, ["dump", "--distilled"])
    assert result.exit_code == 0
    assert "No distilled" in result.output


def test_dump_returns_palace_objects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_exchange(con, "ex1", "conv1", "/fake/a.jsonl")
    _insert_palace(
        con, "p1", "ex1", "connection pool を修正した", "2026-01-15T00:00:00"
    )
    con.close()

    result = runner.invoke(app, ["dump", "--distilled"])
    assert result.exit_code == 0
    assert "connection pool" in result.output


def test_dump_json_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_exchange(con, "ex1", "conv1", "/fake/a.jsonl")
    _insert_palace(con, "p1", "ex1", "pool_size=5 を追加した", "2026-01-15T00:00:00")
    con.close()

    result = runner.invoke(app, ["dump", "--distilled", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["exchange_core"] == "pool_size=5 を追加した"
    assert "rooms" in data[0]
    assert "date" in data[0]


def test_dump_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    for i in range(5):
        _insert_exchange(con, f"ex{i}", f"conv{i}", f"/fake/{i}.jsonl")
        _insert_palace(
            con, f"p{i}", f"ex{i}", f"core {i}", f"2026-01-{15 + i:02d}T00:00:00"
        )
    con.close()

    result = runner.invoke(app, ["dump", "--distilled", "--json", "--limit", "3"])
    data = json.loads(result.output)
    assert len(data) <= 3


def test_dump_newest_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db, con = _setup(tmp_path)
    _insert_exchange(con, "ex1", "conv1", "/fake/a.jsonl")
    _insert_palace(con, "p1", "ex1", "old entry", "2026-01-01T00:00:00")
    _insert_exchange(con, "ex2", "conv2", "/fake/b.jsonl")
    _insert_palace(con, "p2", "ex2", "new entry", "2026-03-01T00:00:00")
    con.close()

    result = runner.invoke(app, ["dump", "--distilled", "--json"])
    data = json.loads(result.output)
    assert data[0]["exchange_core"] == "new entry"
