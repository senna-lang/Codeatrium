"""find_project_root の探索動作と親ディレクトリ通知のテスト"""

from __future__ import annotations

from pathlib import Path

from codeatrium.paths import find_project_root


def _mock_git_root(monkeypatch, root):
    """git_root() を固定値返却にモックする（テスト中は実 git を呼ばない）"""
    monkeypatch.setattr("codeatrium.paths.git_root", lambda: root)


def test_find_project_root_uses_cwd_when_initialized(tmp_path, monkeypatch, capsys):
    """cwd 直下に .codeatrium/ がある場合は cwd を返し、通知を出さない"""
    (tmp_path / ".codeatrium").mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == tmp_path
    assert "parent directory" not in captured.err


def test_find_project_root_walks_to_parent_with_notice(tmp_path, monkeypatch, capsys):
    """サブディレクトリで実行時、親の .codeatrium/ を拾った場合は stderr に通知"""
    (tmp_path / ".codeatrium").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == tmp_path
    assert "parent directory" in captured.err
    assert str(tmp_path) in captured.err


def test_find_project_root_notify_false_suppresses_notice(
    tmp_path, monkeypatch, capsys
):
    """notify=False で親通知を抑止できる"""
    (tmp_path / ".codeatrium").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)

    root = find_project_root(notify=False)
    captured = capsys.readouterr()

    assert root == tmp_path
    assert captured.err == ""


def test_find_project_root_uninitialized_returns_git_root_silently(
    tmp_path, monkeypatch, capsys
):
    """git 内で .codeatrium/ が見つからない場合は git root を返し、通知も出さない

    （呼び出し側が db.exists() で "Not initialized" を出す責務を持つ）
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == tmp_path
    assert captured.err == ""


def test_find_project_root_does_not_cross_git_root(tmp_path, monkeypatch, capsys):
    """別プロジェクト（git root 外）の .codeatrium/ は拾わない"""
    # 親（git 管理外）に .codeatrium/ を配置
    (tmp_path / ".codeatrium").mkdir()

    # 子に独立した git リポジトリ
    inner = tmp_path / "inner_repo"
    inner.mkdir()
    _mock_git_root(monkeypatch, inner)
    monkeypatch.chdir(inner)

    root = find_project_root()
    captured = capsys.readouterr()

    # inner の git root が返り、外の .codeatrium/ は無視される
    assert root == inner
    assert "parent directory" not in captured.err


def test_find_project_root_non_git_does_not_walk_parent(
    tmp_path, monkeypatch, capsys
):
    """git 外のサブディレクトリでは親探索しない"""
    (tmp_path / ".codeatrium").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()

    _mock_git_root(monkeypatch, None)
    monkeypatch.chdir(sub)

    root = find_project_root()
    captured = capsys.readouterr()

    # cwd を返す（親の .codeatrium/ は拾わない）
    assert root == sub
    assert captured.err == ""


def test_resolve_grok_sessions_path_percent_encodes_project_root(
    tmp_path: Path, monkeypatch
) -> None:
    """grok は cwd の絶対パスを percent-encode（`/` も `%2F`）したディレクトリ名を使う。"""
    from urllib.parse import quote

    from codeatrium.paths import resolve_grok_sessions_path

    home = tmp_path / "home"
    project_root = tmp_path / "work" / "myrepo"
    session_dir = home / ".grok" / "sessions" / quote(str(project_root), safe="")
    (session_dir / "019f-abcd").mkdir(parents=True)
    (session_dir / "019f-abcd" / "updates.jsonl").write_text("{}\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert resolve_grok_sessions_path(project_root) == session_dir


def test_resolve_grok_sessions_path_returns_none_for_unknown_project(
    tmp_path: Path, monkeypatch
) -> None:
    """セッションが無いプロジェクトでは None を返す（誤って別プロジェクトを拾わない）。"""
    from codeatrium.paths import resolve_grok_sessions_path

    home = tmp_path / "home"
    (home / ".grok" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert resolve_grok_sessions_path(tmp_path / "work" / "other") is None
