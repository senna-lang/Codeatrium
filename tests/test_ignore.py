"""
`.codeatrium/ignore` の gitignore 風パターンマッチングのテスト（issue #36）
"""

from pathlib import Path

from codeatrium.ignore import load_ignore


def test_load_ignore_returns_empty_matcher_when_file_absent(tmp_path: Path) -> None:
    matcher = load_ignore(tmp_path)
    assert matcher.matches("secrets/api_key.txt") is False


def test_matches_simple_filename_pattern_at_any_depth(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text(".env\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches(".env") is True
    assert matcher.matches("config/.env") is True
    assert matcher.matches("config/.env.local") is False


def test_matches_extension_wildcard(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text("*.pem\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches("certs/server.pem") is True
    assert matcher.matches("server.pem.bak") is False


def test_matches_directory_pattern_only_matches_contents(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text("secrets/\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches("secrets/api_key.txt") is True
    assert matcher.matches("secrets") is False
    assert matcher.matches("mysecrets/x") is False


def test_matches_anchored_pattern_only_at_root(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text("/config.local.toml\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches("config.local.toml") is True
    assert matcher.matches("sub/config.local.toml") is False


def test_matches_double_star_crosses_directories(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text("**/node_modules/**\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches("a/b/node_modules/x/y.js") is True
    assert matcher.matches("node_modules/x.js") is True
    assert matcher.matches("node_modules") is False


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text(
        "# secrets\n\n  \nsecrets/*\n"
    )
    matcher = load_ignore(tmp_path)
    assert matcher.matches("secrets/api_key.txt") is True
    assert matcher.matches("# secrets") is False


def test_negation_re_includes_a_later_pattern(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text("secrets/*\n!secrets/public.txt\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches("secrets/api_key.txt") is True
    assert matcher.matches("secrets/public.txt") is False


def test_matches_any_true_if_one_path_matches(tmp_path: Path) -> None:
    (tmp_path / ".codeatrium").mkdir()
    (tmp_path / ".codeatrium" / "ignore").write_text("secrets/*\n")
    matcher = load_ignore(tmp_path)
    assert matcher.matches_any(["src/foo.py", "secrets/api_key.txt"]) is True
    assert matcher.matches_any(["src/foo.py", "src/bar.py"]) is False
    assert matcher.matches_any([]) is False
