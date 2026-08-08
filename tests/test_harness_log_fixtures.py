"""合成ハーネスログ（tests/fixtures/harness_logs/）が壊れていないことを確認する（design §11.1）"""

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "harness_logs"

_JSONL_FIXTURES = ["claude.jsonl", "codex.jsonl", "omp_pi.jsonl", "grok.jsonl"]
_JSON_FIXTURES = ["opencode.json"]


def test_all_expected_fixture_files_exist() -> None:
    for name in _JSONL_FIXTURES + _JSON_FIXTURES:
        assert (FIXTURES_DIR / name).is_file(), f"missing fixture: {name}"


def test_jsonl_fixtures_are_valid_json_per_line() -> None:
    for name in _JSONL_FIXTURES:
        path = FIXTURES_DIR / name
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines, f"{name} is empty"
        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)


def test_json_fixtures_are_valid_json() -> None:
    for name in _JSON_FIXTURES:
        path = FIXTURES_DIR / name
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)


_REAL_PATH_MARKERS = ("/Users/", "/home/", "/private/var/folders/")


def test_fixtures_contain_no_real_home_directory_paths() -> None:
    """実ログの絶対パス（/Users/<real-user>/... 等）が紛れ込んでいないことを確認する（design §11.1）。

    実行マシンの Path.home() と比較すると、別マシン・CI では常にパスが違うため
    このテストの本来の目的（実ログ混入の検出）で失敗し得ない。マシン非依存の
    パスパターンで判定する。
    """
    for name in _JSONL_FIXTURES + _JSON_FIXTURES:
        text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
        for marker in _REAL_PATH_MARKERS:
            assert marker not in text, f"{name} leaks a real-looking absolute path ({marker})"
