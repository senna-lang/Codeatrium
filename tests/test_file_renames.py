"""file_renames のテスト（design §8.2、ファイル改名の追従・段2）"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeatrium.db import get_connection, init_db
from codeatrium.file_renames import parse_rename_log, resolve_aliases

# ---- parse_rename_log（純関数） ----


def test_parse_rename_log_single_rename() -> None:
    output = "R100\tsrc/logo/db.py\tsrc/codeatrium/db.py\n"
    assert parse_rename_log(output) == [("src/logo/db.py", "src/codeatrium/db.py")]


def test_parse_rename_log_no_renames_returns_empty() -> None:
    assert parse_rename_log("") == []


def test_parse_rename_log_multiple_commits() -> None:
    output = (
        "R100\told_name.py\tmid_name.py\n"
        "\n"
        "R087\tmid_name.py\tnew_name.py\n"
    )
    assert parse_rename_log(output) == [
        ("old_name.py", "mid_name.py"),
        ("mid_name.py", "new_name.py"),
    ]


def test_parse_rename_log_ignores_unrelated_lines() -> None:
    """diff-filter=R を付けているため通常は混在しないが、防御的に無視する"""
    output = "M\tsome/other/file.py\nR100\told.py\tnew.py\n"
    assert parse_rename_log(output) == [("old.py", "new.py")]


# ---- resolve_aliases（git 呼び出し・キャッシュ） ----


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _make_git_repo_with_rename(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "test"], repo)
    (repo / "old_name.py").write_text("def f():\n    return 1\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    _run(["git", "mv", "old_name.py", "new_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "rename"], repo)
    return repo


def _make_git_repo_with_two_hop_rename(tmp_path: Path) -> Path:
    """old_name.py -> mid_name.py -> new_name.py と2回改名した履歴"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "test"], repo)
    (repo / "old_name.py").write_text("def f():\n    return 1\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    _run(["git", "mv", "old_name.py", "mid_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "rename1"], repo)
    _run(["git", "mv", "mid_name.py", "new_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "rename2"], repo)
    return repo


def _setup_db(tmp_path: Path):
    db = tmp_path / "memory.db"
    init_db(db)
    return get_connection(db)


def test_resolve_aliases_finds_renamed_file(tmp_path: Path) -> None:
    repo = _make_git_repo_with_rename(tmp_path)
    con = _setup_db(tmp_path)

    aliases = resolve_aliases(con, str(repo), "new_name.py")

    assert aliases == ["old_name.py"]


def test_resolve_aliases_no_rename_history_returns_empty(tmp_path: Path) -> None:
    repo = _make_git_repo_with_rename(tmp_path)
    con = _setup_db(tmp_path)

    aliases = resolve_aliases(con, str(repo), "never_renamed.py")

    assert aliases == []


def test_resolve_aliases_does_not_write_to_file_renames(tmp_path: Path) -> None:
    """`loci context` は読み取り専用コマンド。Stop hook の loci index と WAL 上で
    競合しないよう、問い合わせ経路からは file_renames へ書き込まない（design §8.2）"""
    repo = _make_git_repo_with_rename(tmp_path)
    con = _setup_db(tmp_path)

    resolve_aliases(con, str(repo), "new_name.py")

    row = con.execute(
        "SELECT 1 FROM file_renames WHERE new_path = ?", ("new_name.py",)
    ).fetchone()
    assert row is None


def test_resolve_aliases_prefers_recorded_row_over_git(tmp_path: Path) -> None:
    """段1（ハーネスの move_path、将来のアダプター実装）が記録した行があれば
    それを使い、git は呼ばない——git 呼び出し不能な project_root でも解決できる"""
    con = _setup_db(tmp_path)
    con.execute(
        "INSERT INTO file_renames (old_path, new_path, source, ts) VALUES (?, ?, 'harness', ?)",
        ("old_name.py", "new_name.py", "2026-08-09T00:00:00Z"),
    )
    con.commit()

    aliases = resolve_aliases(con, str(tmp_path / "does-not-exist"), "new_name.py")

    assert aliases == ["old_name.py"]


def test_resolve_aliases_two_hop_rename_chain(tmp_path: Path) -> None:
    """2回改名されたファイルは、両方の旧パスをエイリアスとして返す"""
    repo = _make_git_repo_with_two_hop_rename(tmp_path)
    con = _setup_db(tmp_path)

    aliases = resolve_aliases(con, str(repo), "new_name.py")

    assert aliases == ["mid_name.py", "old_name.py"]


def test_resolve_aliases_returns_empty_when_git_unavailable(tmp_path: Path) -> None:
    """リポジトリでない・git が使えない場合は例外を投げず空リストを返す（§3.3）"""
    con = _setup_db(tmp_path)
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    aliases = resolve_aliases(con, str(non_repo), "whatever.py")

    assert aliases == []


def test_resolve_aliases_handles_non_ascii_renamed_path(tmp_path: Path) -> None:
    """git のデフォルト `core.quotepath=true` は非ASCIIパスを `git log --name-status`
    で `"caf\\303\\251.py"` のように8進数エスケープした二重引用符付き文字列で出力する。
    `-c core.quotepath=false` を付けないと resolve_aliases が返す旧パスがエスケープ
    されたままで実際のファイル名と一致しない（issue #19）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "test"], repo)
    (repo / "café.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    _run(["git", "mv", "café.py", "new_name.py"], repo)
    _run(["git", "commit", "-q", "-m", "rename"], repo)
    con = _setup_db(tmp_path)

    aliases = resolve_aliases(con, str(repo), "new_name.py")

    assert aliases == ["café.py"]
