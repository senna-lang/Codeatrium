"""loci eval CLI — gen/run/report wiring and error paths (E6)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from codeatrium.cli import app
from codeatrium.db import get_connection, init_db
from codeatrium.eval.datasets.schema import Query, dump_dataset

runner = CliRunner()


def _init_project(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    db = tmp_path / ".codeatrium" / "memory.db"
    init_db(db)
    return db


def _seed_symbol_exchange(db: Path) -> None:
    con = get_connection(db)
    con.execute("INSERT INTO conversations (id, source_path) VALUES ('c1', '/p')")
    con.execute(
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) "
        "VALUES ('ex1', 'c1', 0, 1, 'fix list_dir', 'done')"
    )
    con.execute(
        "INSERT INTO code_symbols (id, file_path, symbol_name, symbol_kind, signature, line, end_line, lang, resolved_at) "
        "VALUES ('sym1', 'src/foo.py', 'list_dir', 'function', 'def list_dir():', 1, 2, '.py', '2026-01-01')"
    )
    con.execute(
        "INSERT INTO code_edges (id, exchange_id, file_path, symbol_id, edge_kind, granularity, confidence, added, ts) "
        "VALUES ('edge1', 'ex1', 'src/foo.py', 'sym1', 'edit', 'line', 1.0, 3, '2026-01-01')"
    )
    con.commit()
    con.close()


# ---- run ----


def test_eval_run_reports_symbol_adapter_in_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _init_project(tmp_path)
    _seed_symbol_exchange(db)

    dataset_path = tmp_path / "datasets" / "symbol-recall.v0.jsonl"
    dump_dataset(
        [Query(id="q1", kind="symbol", value="src/foo.py::list_dir", gold_exchange_ids=("ex1",))],
        dataset_path,
    )
    monkeypatch.setattr("codeatrium.eval.datasets.schema.DATASETS_DIR", dataset_path.parent)

    result = runner.invoke(
        app, ["eval", "run", "--dataset", "symbol-recall", "--adapter", "symbol", "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["corpus"]["query_count"] == 1
    assert payload["adapters"][0]["adapter_id"] == "symbol"
    assert payload["adapters"][0]["mrr_at_10"] == 1.0


def test_eval_run_missing_dataset_file_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)
    monkeypatch.setattr(
        "codeatrium.eval.datasets.schema.DATASETS_DIR", tmp_path / "empty-datasets"
    )

    result = runner.invoke(app, ["eval", "run", "--dataset", "symbol-recall"])

    assert result.exit_code != 0
    assert "loci eval gen" in result.output


def test_eval_run_unknown_adapter_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _init_project(tmp_path)
    _seed_symbol_exchange(db)
    dataset_path = tmp_path / "datasets" / "symbol-recall.v0.jsonl"
    dump_dataset(
        [Query(id="q1", kind="symbol", value="src/foo.py::list_dir", gold_exchange_ids=("ex1",))],
        dataset_path,
    )
    monkeypatch.setattr("codeatrium.eval.datasets.schema.DATASETS_DIR", dataset_path.parent)

    result = runner.invoke(app, ["eval", "run", "--dataset", "symbol-recall", "--adapter", "bogus"])

    assert result.exit_code != 0
    assert "Unknown adapter" in result.output


def test_eval_run_requires_initialized_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    result = runner.invoke(app, ["eval", "run", "--dataset", "symbol-recall"])

    assert result.exit_code != 0
    assert "loci init" in result.output


# ---- gen ----


def test_eval_gen_unsupported_dataset_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)

    result = runner.invoke(app, ["eval", "gen", "--dataset", "bogus"])

    assert result.exit_code != 0
    assert "Unsupported dataset" in result.output


def test_eval_gen_writes_symbol_recall_dataset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def list_dir():\n    pass\n")
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)

    db = tmp_path / ".codeatrium" / "memory.db"
    init_db(db)
    con = get_connection(db)
    con.execute("INSERT INTO conversations (id, source_path) VALUES ('c1', '/p')")
    con.execute(
        "INSERT INTO exchange_files (exchange_id, file_path) VALUES ('ex1', 'src/foo.py')"
    )
    con.execute(
        "INSERT INTO code_touches (id, exchange_id, harness, tool_call_id, file_path, touch_kind, locator_kind) "
        "VALUES ('t1', 'ex1', 'claude', 'call1', 'src/foo.py', 'edit', 'file_only')"
    )
    con.execute(
        "INSERT INTO exchanges (id, conversation_id, ply_start, ply_end, user_content, agent_content) "
        "VALUES ('ex1', 'c1', 0, 1, 'fix list_dir please', 'done')"
    )
    con.commit()
    con.close()

    out_dir = tmp_path / "datasets"
    monkeypatch.setattr("codeatrium.eval.datasets.schema.DATASETS_DIR", out_dir)

    result = runner.invoke(app, ["eval", "gen", "--dataset", "symbol-recall"])

    assert result.exit_code == 0, result.output
    out_path = out_dir / "symbol-recall.v0.jsonl"
    assert out_path.exists()
    lines = [json.loads(line) for line in out_path.read_text().splitlines() if line]
    assert any(row["value"] == "src/foo.py::list_dir" for row in lines)


# ---- report ----


def test_eval_report_delegates_to_run_with_all_adapters_markdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)

    calls: list[dict] = []

    def _fake_eval_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("codeatrium.cli.eval_cmd.eval_run", _fake_eval_run)

    result = runner.invoke(app, ["eval", "report", "--dataset", "symbol-recall"])

    assert result.exit_code == 0
    assert calls == [
        {
            "dataset": "symbol-recall",
            "adapter": "all",
            "k": 10,
            "seed": 42,
            "json_output": False,
        }
    ]
